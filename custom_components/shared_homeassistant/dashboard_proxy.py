"""Dashboard proxy for Shared Home Assistant.

Proxies a remote HA instance's frontend through the local instance,
injecting authentication automatically. This allows embedding remote
dashboards in iframes without cross-origin or auth issues.

SECURITY NOTE: Auth tokens are exchanged via MQTT. Ensure your MQTT
broker uses authentication and TLS encryption.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp
from aiohttp import web, WSMsgType

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    TOPIC_PREFIX,
    TOPIC_DASHBOARD_INFO,
    TOPIC_SUB_DASHBOARD_INFO,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_URL,
    CONF_SHARE_DASHBOARDS,
    CONF_SHARED_DASHBOARD_LIST,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)

# Proxy base path
PROXY_PATH = "/api/shared_ha/proxy"


class DashboardProxy:
    """Manages dashboard sharing: token exchange via MQTT and HTTP proxy."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: MQTTClient,
        instance_id: str,
        config: dict[str, Any],
    ) -> None:
        """Initialize the dashboard proxy."""
        self._hass = hass
        self._mqtt = mqtt_client
        self._instance_id = instance_id
        self._instance_url: str = config.get(CONF_INSTANCE_URL, "")
        self._share_dashboards: bool = config.get(CONF_SHARE_DASHBOARDS, False)
        self._shared_dashboard_list: list[str] = config.get(
            CONF_SHARED_DASHBOARD_LIST, []
        )

        # Remote instance data: {instance_id: {url, token, name, dashboards}}
        self._remote_instances: dict[str, dict[str, Any]] = {}
        self._auth_token: str | None = None
        self._views_registered = False

    async def async_start(self) -> None:
        """Start dashboard proxy services."""
        # Subscribe to dashboard info from other instances
        await self._mqtt.async_subscribe(
            TOPIC_SUB_DASHBOARD_INFO, self._handle_dashboard_info
        )

        # If we're sharing dashboards, generate token and publish
        if self._share_dashboards and self._instance_url:
            await self._generate_and_publish_token()

        # Register proxy HTTP views
        if not self._views_registered:
            self._hass.http.register_view(DashboardProxyHTTPView(self))
            self._hass.http.register_view(DashboardProxyWSView(self))
            self._views_registered = True

    async def async_stop(self) -> None:
        """Stop dashboard proxy services."""
        await self._mqtt.async_unsubscribe(TOPIC_SUB_DASHBOARD_INFO)

        # Publish empty dashboard info to clear
        if self._share_dashboards:
            topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
            await self._mqtt.async_publish(topic, "", retain=True)

    async def _generate_and_publish_token(self) -> None:
        """Generate a long-lived token and publish dashboard info via MQTT."""
        # Generate a token using HA's auth system
        try:
            user = await self._hass.auth.async_get_owner()
            if user is None:
                # Fall back to first admin user
                for u in await self._hass.auth.async_get_users():
                    if u.is_owner or u.is_admin:
                        user = u
                        break

            if user is None:
                _LOGGER.error("No admin user found, cannot generate dashboard token")
                return

            # Create a refresh token for this integration
            refresh_token = await self._hass.auth.async_create_refresh_token(
                user,
                client_name=f"Shared HA Dashboard ({self._instance_id[:8]})",
                token_type="long_lived_access_token",
                access_token_expiration=365 * 24 * 3600,  # 1 year
            )
            self._auth_token = self._hass.auth.async_create_access_token(
                refresh_token
            )
        except Exception:
            _LOGGER.exception("Failed to generate dashboard access token")
            return

        # Get list of available dashboards
        dashboards = await self._get_dashboard_list()

        # Publish dashboard info with token
        payload = {
            "instance_id": self._instance_id,
            "url": self._instance_url,
            "token": self._auth_token,
            "dashboards": dashboards,
        }

        topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info(
            "Published dashboard info with %d dashboards", len(dashboards)
        )

    async def _get_dashboard_list(self) -> list[dict[str, str]]:
        """Get list of dashboards from Lovelace."""
        try:
            from homeassistant.components.lovelace.const import LOVELACE_DATA

            lovelace_data = self._hass.data.get(LOVELACE_DATA)
            if lovelace_data is None:
                return []

            result = []
            for url_path, dashboard in lovelace_data.dashboards.items():
                if url_path is None:
                    # Default dashboard
                    if not self._shared_dashboard_list or "lovelace" in self._shared_dashboard_list:
                        result.append({
                            "url_path": "lovelace",
                            "title": "Overview",
                            "icon": "mdi:view-dashboard",
                        })
                else:
                    if not self._shared_dashboard_list or url_path in self._shared_dashboard_list:
                        # Get dashboard metadata
                        title = url_path
                        icon = "mdi:view-dashboard"
                        if hasattr(dashboard, "config") and isinstance(
                            dashboard.config, dict
                        ):
                            title = dashboard.config.get("title", url_path)
                            icon = dashboard.config.get("icon", icon)

                        result.append({
                            "url_path": url_path,
                            "title": title,
                            "icon": icon,
                        })

            return result
        except Exception:
            _LOGGER.exception("Failed to get dashboard list")
            return []

    async def _handle_dashboard_info(self, topic: str, payload: bytes) -> None:
        """Handle incoming dashboard info from other instances."""
        parts = topic.split("/")
        if len(parts) < 3:
            return

        instance_id = parts[1]
        if instance_id == self._instance_id:
            return

        if not payload:
            # Instance removed dashboard sharing
            self._remote_instances.pop(instance_id, None)
            self._remove_panels(instance_id)
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        self._remote_instances[instance_id] = {
            "url": data.get("url", ""),
            "token": data.get("token", ""),
            "dashboards": data.get("dashboards", []),
        }

        _LOGGER.info(
            "Discovered %d dashboards from instance %s",
            len(data.get("dashboards", [])),
            instance_id[:8],
        )

        # Register sidebar panels for discovered dashboards
        await self._register_panels(instance_id)

    async def _register_panels(self, instance_id: str) -> None:
        """Register sidebar panels for remote dashboards."""
        from homeassistant.components import frontend

        info = self._remote_instances.get(instance_id)
        if not info:
            return

        for dashboard in info.get("dashboards", []):
            url_path = dashboard.get("url_path", "")
            title = dashboard.get("title", url_path)
            icon = dashboard.get("icon", "mdi:view-dashboard")

            panel_url_path = f"shared-{instance_id[:8]}-{url_path}"

            frontend.async_register_built_in_panel(
                self._hass,
                component_name="iframe",
                sidebar_title=title,
                sidebar_icon=icon,
                frontend_url_path=panel_url_path,
                config={
                    "url": f"{PROXY_PATH}/{instance_id}/{url_path}?kiosk"
                },
                require_admin=False,
                update=True,
            )

            _LOGGER.info(
                "Registered dashboard panel '%s' from instance %s",
                title,
                instance_id[:8],
            )

    def _remove_panels(self, instance_id: str) -> None:
        """Remove sidebar panels for a remote instance."""
        from homeassistant.components import frontend

        info = self._remote_instances.get(instance_id)
        if not info:
            return

        for dashboard in info.get("dashboards", []):
            url_path = dashboard.get("url_path", "")
            panel_url_path = f"shared-{instance_id[:8]}-{url_path}"
            try:
                frontend.async_remove_panel(self._hass, panel_url_path)
            except Exception:
                pass

    def get_remote_info(self, instance_id: str) -> dict[str, Any] | None:
        """Get remote instance connection info for the proxy views."""
        return self._remote_instances.get(instance_id)


class DashboardProxyHTTPView(HomeAssistantView):
    """Proxy HTTP requests to a remote HA instance."""

    url = f"{PROXY_PATH}/{{instance_id}}/{{path:.*}}"
    name = "api:shared_ha:proxy"
    requires_auth = True

    def __init__(self, proxy: DashboardProxy) -> None:
        """Initialize the proxy view."""
        self._proxy = proxy

    async def get(self, request: web.Request, instance_id: str, path: str) -> web.StreamResponse:
        """Proxy GET requests."""
        return await self._proxy_request(request, instance_id, path, "GET")

    async def post(self, request: web.Request, instance_id: str, path: str) -> web.StreamResponse:
        """Proxy POST requests."""
        return await self._proxy_request(request, instance_id, path, "POST")

    async def _proxy_request(
        self,
        request: web.Request,
        instance_id: str,
        path: str,
        method: str,
    ) -> web.StreamResponse:
        """Proxy an HTTP request to the remote instance."""
        info = self._proxy.get_remote_info(instance_id)
        if not info:
            return web.Response(status=404, text="Unknown instance")

        remote_url = info["url"].rstrip("/")
        token = info["token"]

        # Build target URL
        target_url = f"{remote_url}/{path}"
        if request.query_string:
            target_url += f"?{request.query_string}"

        # Build headers — inject auth, forward relevant headers
        headers = {
            "Authorization": f"Bearer {token}",
        }
        for header in ("Accept", "Content-Type", "Accept-Encoding"):
            if header in request.headers:
                headers[header] = request.headers[header]

        # Read request body if POST
        body = None
        if method == "POST":
            body = await request.read()

        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method,
                    target_url,
                    headers=headers,
                    data=body,
                    ssl=False,  # Don't verify remote cert
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    # Stream the response back
                    response = web.StreamResponse(
                        status=resp.status,
                        headers={
                            k: v
                            for k, v in resp.headers.items()
                            if k.lower()
                            not in (
                                "transfer-encoding",
                                "content-encoding",
                                "content-length",
                                "x-frame-options",
                                "content-security-policy",
                            )
                        },
                    )
                    response.content_type = resp.content_type or "application/octet-stream"
                    await response.prepare(request)

                    async for chunk in resp.content.iter_chunked(8192):
                        await response.write(chunk)

                    await response.write_eof()
                    return response
        except aiohttp.ClientError as err:
            _LOGGER.warning("Proxy request failed for %s: %s", target_url, err)
            return web.Response(status=502, text=f"Proxy error: {err}")


class DashboardProxyWSView(HomeAssistantView):
    """Proxy WebSocket connections to a remote HA instance."""

    url = f"{PROXY_PATH}/{{instance_id}}/api/websocket"
    name = "api:shared_ha:proxy:ws"
    requires_auth = True

    def __init__(self, proxy: DashboardProxy) -> None:
        """Initialize the WS proxy view."""
        self._proxy = proxy

    async def get(self, request: web.Request, instance_id: str) -> web.WebSocketResponse:
        """Handle WebSocket upgrade and proxy."""
        info = self._proxy.get_remote_info(instance_id)
        if not info:
            return web.Response(status=404, text="Unknown instance")

        remote_url = info["url"].rstrip("/")
        token = info["token"]

        # Convert http(s) to ws(s)
        ws_url = remote_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url += "/api/websocket"

        # Accept the local WebSocket connection
        local_ws = web.WebSocketResponse()
        await local_ws.prepare(request)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    ws_url, ssl=False, timeout=30
                ) as remote_ws:
                    # Handle the HA auth handshake with the remote
                    # Remote sends auth_required, we respond with token
                    auth_msg = await remote_ws.receive_json()
                    if auth_msg.get("type") == "auth_required":
                        await remote_ws.send_json(
                            {"type": "auth", "access_token": token}
                        )
                        auth_result = await remote_ws.receive_json()
                        if auth_result.get("type") != "auth_ok":
                            await local_ws.close(
                                code=4001, message=b"Remote auth failed"
                            )
                            return local_ws

                    # Forward the auth_required and auth_ok to local client
                    # so the HA frontend initializes correctly
                    await local_ws.send_json({"type": "auth_ok"})

                    # Bidirectional proxy
                    async def forward_local_to_remote():
                        async for msg in local_ws:
                            if msg.type == WSMsgType.TEXT:
                                await remote_ws.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await remote_ws.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break

                    async def forward_remote_to_local():
                        async for msg in remote_ws:
                            if msg.type == WSMsgType.TEXT:
                                await local_ws.send_str(msg.data)
                            elif msg.type == WSMsgType.BINARY:
                                await local_ws.send_bytes(msg.data)
                            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                                break

                    # Run both directions concurrently
                    await asyncio.gather(
                        forward_local_to_remote(),
                        forward_remote_to_local(),
                        return_exceptions=True,
                    )
        except Exception:
            _LOGGER.debug("WebSocket proxy closed for instance %s", instance_id[:8])
        finally:
            if not local_ws.closed:
                await local_ws.close()

        return local_ws
