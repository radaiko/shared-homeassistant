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
import re
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
    CONF_INSTANCE_URL,
    CONF_SHARE_DASHBOARDS,
    CONF_SHARED_DASHBOARD_LIST,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)

# Proxy base path
PROXY_PATH = "/api/shared_ha/proxy"
_VIEW_KEY = f"{DOMAIN}_proxy_views_registered"

# Headers to strip from proxied requests/responses
_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "te", "trailers",
    "transfer-encoding", "upgrade",
})
_STRIP_REQUEST = _HOP_BY_HOP | {"host", "content-length", "authorization"}
_STRIP_RESPONSE = _HOP_BY_HOP | {
    "content-encoding", "content-length",
    "x-frame-options", "content-security-policy",
}

# Known HA frontend absolute paths that need rewriting
_HA_PATHS = (
    "frontend_latest", "static", "hacsfiles", "local",
    "api", "auth", "lovelace", "config", "logbook",
    "history", "map", "developer-tools", "profile",
)
_HA_PATHS_PATTERN = "|".join(_HA_PATHS)

# Regex for absolute-path HTML attributes
_ABS_ATTR_RE = re.compile(
    rb'((?:src|href|action|data-src)=["\'])'
    rb'(/(?:' + _HA_PATHS_PATTERN.encode() + rb')[^"\']*)',
    re.IGNORECASE,
)

# Regex for absolute paths inside inline <script> strings
_URL_IN_SCRIPT_RE = re.compile(
    rb"""(["'])(/(?:""" + _HA_PATHS_PATTERN.encode() + rb""")[^"']*)""",
)

# Regex to find <head> tag for base injection
_HEAD_TAG_RE = re.compile(rb"(<head[^>]*>)", re.IGNORECASE)

# Max body size to read into memory for rewriting (10 MB)
_MAX_REWRITE_SIZE = 10 * 1024 * 1024


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

        # Remote instance data: {instance_id: {url, token, session, dashboards}}
        self._remote_instances: dict[str, dict[str, Any]] = {}
        self._auth_token: str | None = None

    async def async_register_subscriptions(self) -> None:
        """Pre-register MQTT subscriptions (before connect)."""
        await self._mqtt.async_subscribe(
            TOPIC_SUB_DASHBOARD_INFO, self._handle_dashboard_info
        )

    async def async_start(self) -> None:
        """Start dashboard proxy services (subscriptions already registered)."""
        # If we're sharing dashboards, generate token and publish
        if self._share_dashboards and self._instance_url:
            await self._generate_and_publish_token()

        # Register proxy HTTP views (only once per HA lifetime)
        if not self._hass.data.get(_VIEW_KEY):
            try:
                # Register WS view first (more specific path)
                self._hass.http.register_view(DashboardProxyWSView(self))
                self._hass.http.register_view(DashboardProxyHTTPView(self))
            except ValueError:
                # Views already registered from a previous load
                _LOGGER.debug("Proxy views already registered")
            self._hass.data[_VIEW_KEY] = True

    async def async_stop(self) -> None:
        """Stop dashboard proxy services."""
        await self._mqtt.async_unsubscribe(TOPIC_SUB_DASHBOARD_INFO)

        # Publish empty dashboard info to clear
        if self._share_dashboards:
            topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
            await self._mqtt.async_publish(topic, "", retain=True)

        # Close all client sessions
        for info in self._remote_instances.values():
            session = info.get("session")
            if session and not session.closed:
                await session.close()

    async def _generate_and_publish_token(self) -> None:
        """Generate a long-lived token and publish dashboard info via MQTT."""
        try:
            user = await self._hass.auth.async_get_owner()
            if user is None:
                for u in await self._hass.auth.async_get_users():
                    if u.is_owner or u.is_admin:
                        user = u
                        break

            if user is None:
                _LOGGER.error("No admin user found, cannot generate dashboard token")
                return

            refresh_token = await self._hass.auth.async_create_refresh_token(
                user,
                client_name=f"Shared HA Dashboard ({self._instance_id[:8]})",
                token_type="long_lived_access_token",
                access_token_expiration=365 * 24 * 3600,
            )
            self._auth_token = self._hass.auth.async_create_access_token(
                refresh_token
            )
        except Exception:
            _LOGGER.exception("Failed to generate dashboard access token")
            return

        dashboards = await self._get_dashboard_list()

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
                    if not self._shared_dashboard_list or "lovelace" in self._shared_dashboard_list:
                        result.append({
                            "url_path": "lovelace",
                            "title": "Overview",
                            "icon": "mdi:view-dashboard",
                        })
                else:
                    if not self._shared_dashboard_list or url_path in self._shared_dashboard_list:
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
            self._remove_panels(instance_id)
            old = self._remote_instances.pop(instance_id, None)
            if old and old.get("session") and not old["session"].closed:
                await old["session"].close()
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Create a persistent client session for this remote instance
        old = self._remote_instances.get(instance_id)
        if old and old.get("session") and not old["session"].closed:
            session = old["session"]
        else:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=60, connect=10),
            )

        self._remote_instances[instance_id] = {
            "url": data.get("url", ""),
            "token": data.get("token", ""),
            "dashboards": data.get("dashboards", []),
            "session": session,
        }

        _LOGGER.info(
            "Discovered %d dashboards from instance %s",
            len(data.get("dashboards", [])),
            instance_id[:8],
        )

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


def _build_request_headers(request: web.Request, token: str) -> dict[str, str]:
    """Build headers for the proxied request."""
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST
    }
    headers["Authorization"] = f"Bearer {token}"
    if request.remote:
        headers["X-Forwarded-For"] = request.remote
    headers["X-Forwarded-Proto"] = request.scheme
    return headers


def _build_response_headers(resp: aiohttp.ClientResponse) -> dict[str, str]:
    """Build headers for the response back to the client."""
    return {
        k: v for k, v in resp.headers.items()
        if k.lower() not in _STRIP_RESPONSE
    }


def _rewrite_html(body: bytes, instance_id: str) -> bytes:
    """Rewrite absolute paths in HTML to go through the proxy."""
    prefix = f"{PROXY_PATH}/{instance_id}".encode()

    # Inject <base href> after <head> tag
    base_tag = f'<base href="{PROXY_PATH}/{instance_id}/">'.encode()
    body = _HEAD_TAG_RE.sub(lambda m: m.group(0) + base_tag, body, count=1)

    # Rewrite absolute-path HTML attributes
    body = _ABS_ATTR_RE.sub(lambda m: m.group(1) + prefix + m.group(2), body)

    return body


def _rewrite_js(body: bytes, instance_id: str) -> bytes:
    """Rewrite absolute paths in JS to go through the proxy."""
    prefix = f"{PROXY_PATH}/{instance_id}".encode()
    return _URL_IN_SCRIPT_RE.sub(lambda m: m.group(1) + prefix + m.group(2), body)


class DashboardProxyHTTPView(HomeAssistantView):
    """Proxy HTTP requests to a remote HA instance."""

    url = f"{PROXY_PATH}/{{instance_id}}/{{path:.*}}"
    name = "api:shared_ha:proxy"
    requires_auth = True

    def __init__(self, proxy: DashboardProxy) -> None:
        """Initialize the proxy view."""
        self._proxy = proxy

    async def _handle(
        self, request: web.Request, instance_id: str, path: str
    ) -> web.StreamResponse:
        """Proxy an HTTP request to the remote instance."""
        info = self._proxy.get_remote_info(instance_id)
        if not info:
            return web.Response(status=404, text="Unknown instance")

        remote_url = info["url"].rstrip("/")
        token = info["token"]
        session = info["session"]

        target_url = f"{remote_url}/{path}"
        if request.query_string:
            target_url += f"?{request.query_string}"

        req_headers = _build_request_headers(request, token)

        body = None
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.read()

        try:
            async with session.request(
                method=request.method,
                url=target_url,
                headers=req_headers,
                data=body,
                allow_redirects=False,
            ) as resp:
                resp_headers = _build_response_headers(resp)
                content_type = resp.headers.get("Content-Type", "")

                # Handle 304 Not Modified (no body)
                if resp.status == 304:
                    return web.Response(status=304, headers=resp_headers)

                # Handle redirects — rewrite Location header
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if location.startswith("/"):
                        resp_headers["Location"] = (
                            f"{PROXY_PATH}/{instance_id}{location}"
                        )
                    return web.Response(status=resp.status, headers=resp_headers)

                # For HTML responses, read and rewrite
                if "text/html" in content_type:
                    raw = await resp.read()
                    if len(raw) <= _MAX_REWRITE_SIZE:
                        raw = _rewrite_html(raw, instance_id)
                    return web.Response(
                        status=resp.status,
                        headers=resp_headers,
                        body=raw,
                        content_type="text/html",
                    )

                # For JS responses, rewrite absolute paths
                if "javascript" in content_type:
                    raw = await resp.read()
                    if len(raw) <= _MAX_REWRITE_SIZE:
                        raw = _rewrite_js(raw, instance_id)
                    return web.Response(
                        status=resp.status,
                        headers=resp_headers,
                        body=raw,
                        content_type=content_type,
                    )

                # Stream all other responses
                response = web.StreamResponse(
                    status=resp.status, headers=resp_headers
                )
                response.content_type = (
                    resp.content_type or "application/octet-stream"
                )
                await response.prepare(request)
                async for chunk in resp.content.iter_chunked(65536):
                    await response.write(chunk)
                await response.write_eof()
                return response

        except aiohttp.ClientError as err:
            _LOGGER.warning("Proxy request failed for %s: %s", target_url, err)
            return web.Response(status=502, text=f"Proxy error: {err}")

    # Wire all HTTP methods
    get = post = put = delete = patch = options = _handle


class DashboardProxyWSView(HomeAssistantView):
    """Proxy WebSocket connections to a remote HA instance."""

    url = f"{PROXY_PATH}/{{instance_id}}/api/websocket"
    name = "api:shared_ha:proxy:ws"
    requires_auth = True

    def __init__(self, proxy: DashboardProxy) -> None:
        """Initialize the WS proxy view."""
        self._proxy = proxy

    async def get(
        self, request: web.Request, instance_id: str
    ) -> web.WebSocketResponse:
        """Handle WebSocket upgrade and proxy."""
        info = self._proxy.get_remote_info(instance_id)
        if not info:
            return web.Response(status=404, text="Unknown instance")

        remote_url = info["url"].rstrip("/")
        token = info["token"]
        session = info["session"]

        # Convert http(s) to ws(s)
        ws_url = remote_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url += "/api/websocket"

        local_ws = web.WebSocketResponse(heartbeat=30)
        await local_ws.prepare(request)

        try:
            remote_ws = await session.ws_connect(ws_url, heartbeat=30)

            # Handle HA auth handshake with the remote
            auth_msg = await remote_ws.receive_json()
            if auth_msg.get("type") == "auth_required":
                await remote_ws.send_json(
                    {"type": "auth", "access_token": token}
                )
                auth_result = await remote_ws.receive_json()
                if auth_result.get("type") != "auth_ok":
                    await local_ws.close(code=4001, message=b"Remote auth failed")
                    return local_ws

            # Send auth_ok to the local frontend
            await local_ws.send_json({"type": "auth_ok"})

            # Bidirectional proxy
            async def forward(src, dst):
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            await asyncio.gather(
                forward(local_ws, remote_ws),
                forward(remote_ws, local_ws),
                return_exceptions=True,
            )

            if not remote_ws.closed:
                await remote_ws.close()
        except Exception:
            _LOGGER.debug(
                "WebSocket proxy closed for instance %s", instance_id[:8]
            )
        finally:
            if not local_ws.closed:
                await local_ws.close()

        return local_ws
