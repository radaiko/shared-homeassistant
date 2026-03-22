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
        self._instance_name: str = config.get("instance_name", "")
        self._share_dashboards: bool = config.get(CONF_SHARE_DASHBOARDS, False)
        self._shared_dashboard_list: list[str] = config.get(
            CONF_SHARED_DASHBOARD_LIST, []
        )

        # Remote instance data: {instance_id: {url, token, session, dashboards}}
        self._remote_instances: dict[str, dict[str, Any]] = {}

    async def async_update_config(self, config: dict[str, Any]) -> None:
        """Update config and re-publish dashboard info if needed."""
        self._instance_url = config.get(CONF_INSTANCE_URL, "")
        self._share_dashboards = config.get(CONF_SHARE_DASHBOARDS, False)
        self._shared_dashboard_list = config.get(CONF_SHARED_DASHBOARD_LIST, [])

        if self._share_dashboards and self._instance_url:
            await self._publish_dashboard_info()
        else:
            # Clear published dashboard info
            topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
            await self._mqtt.async_publish(topic, "", retain=True)

    async def async_register_subscriptions(self) -> None:
        """Pre-register MQTT subscriptions (before connect)."""
        await self._mqtt.async_subscribe(
            TOPIC_SUB_DASHBOARD_INFO, self._handle_dashboard_info
        )

    async def async_start(self) -> None:
        """Start dashboard proxy services (subscriptions already registered)."""
        # If we're sharing dashboards, generate token and publish
        if self._share_dashboards and self._instance_url:
            await self._publish_dashboard_info()

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


    async def _publish_dashboard_info(self) -> None:
        """Publish available dashboards and auth token to MQTT.

        The token is needed by the receiving instance's proxy to authenticate
        WebSocket connections to this instance. It is NOT used by the browser.
        """
        # Generate a token for the proxy to use
        token = await self._generate_token()

        dashboards = await self._get_dashboard_list()

        payload = {
            "instance_id": self._instance_id,
            "instance_name": self._instance_name,
            "url": self._instance_url,
            "token": token or "",
            "dashboards": dashboards,
        }

        topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=True)
        _LOGGER.info(
            "Published dashboard info with %d dashboards", len(dashboards)
        )

    async def _generate_token(self) -> str | None:
        """Generate a long-lived access token for proxy authentication."""
        try:
            user = await self._hass.auth.async_get_owner()
            if user is None:
                for u in await self._hass.auth.async_get_users():
                    if u.is_owner or u.is_admin:
                        user = u
                        break

            if user is None:
                _LOGGER.error("No admin user found, cannot generate dashboard token")
                return None

            from datetime import timedelta

            refresh_token = await self._hass.auth.async_create_refresh_token(
                user,
                client_name=f"Shared HA Dashboard ({self._instance_id[:8]})",
                token_type="long_lived_access_token",
                access_token_expiration=timedelta(days=365),
            )
            return self._hass.auth.async_create_access_token(refresh_token)
        except Exception:
            _LOGGER.exception("Failed to generate dashboard access token")
            return None

    async def _get_dashboard_list(self) -> list[dict[str, str]]:
        """Get list of dashboards from Lovelace."""
        try:
            from homeassistant.components.lovelace.const import LOVELACE_DATA

            lovelace_data = self._hass.data.get(LOVELACE_DATA)
            if lovelace_data is None:
                return []

            result = []
            seen = set()
            for url_path, dashboard in lovelace_data.dashboards.items():
                effective_path = url_path or "lovelace"
                if effective_path in seen:
                    continue
                seen.add(effective_path)

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
            self._remote_instances.pop(instance_id, None)
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        self._remote_instances[instance_id] = {
            "url": data.get("url", ""),
            "token": data.get("token", ""),
            "instance_name": data.get("instance_name", ""),
            "dashboards": data.get("dashboards", []),
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

        instance_name = info.get("instance_name", instance_id[:8])

        for dashboard in info.get("dashboards", []):
            url_path = dashboard.get("url_path", "")
            title = dashboard.get("title", url_path)
            icon = dashboard.get("icon", "mdi:view-dashboard")

            # For the default dashboard, use instance name instead of "Overview"
            if url_path == "lovelace" and instance_name:
                sidebar_title = instance_name.title()
            else:
                sidebar_title = title

            panel_url_path = f"shared-{instance_id[:8]}-{url_path}"

            # Point directly to the remote HA's dashboard URL
            # This works when both instances use HTTPS (no mixed content)
            remote_url = info.get("url", "").rstrip("/")
            if url_path == "lovelace":
                dashboard_url = f"{remote_url}/?kiosk"
            else:
                dashboard_url = f"{remote_url}/{url_path}?kiosk"

            frontend.async_register_built_in_panel(
                self._hass,
                component_name="iframe",
                sidebar_title=sidebar_title,
                sidebar_icon=icon,
                frontend_url_path=panel_url_path,
                config={"url": dashboard_url},
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
        if k.lower() not in _STRIP_RESPONSE and k.lower() != "content-type"
    }


def _rewrite_html_proxy(body: bytes, instance_id: str, original_path: str, token: str = "") -> bytes:
    """Rewrite HTML for proxy: replace all absolute paths and inject overrides."""
    prefix = f"{PROXY_PATH}/{instance_id}".encode()
    proxy_prefix = f"{PROXY_PATH}/{instance_id}"

    # Rewrite absolute-path HTML attributes
    body = _ABS_ATTR_RE.sub(lambda m: m.group(1) + prefix + m.group(2), body)

    # Rewrite /manifest.json
    body = body.replace(
        b'href="/manifest.json"',
        f'href="{proxy_prefix}/manifest.json"'.encode(),
    )

    # Inject script that overrides WebSocket, fetch, XHR to go through proxy,
    # and navigates to the correct dashboard after load
    proxy_script = f"""<script>
    (function() {{
        var P = "{proxy_prefix}";

        // Isolate localStorage to prevent the proxied HA frontend from
        // overwriting the parent instance's auth tokens. We use a prefixed
        // key namespace so both can coexist on the same origin.
        var origLS = window.localStorage;
        var lsPrefix = "shared_ha_proxy_";
        var proxyStorage = {{
            getItem: function(key) {{ return origLS.getItem(lsPrefix + key); }},
            setItem: function(key, val) {{ origLS.setItem(lsPrefix + key, val); }},
            removeItem: function(key) {{ origLS.removeItem(lsPrefix + key); }},
            clear: function() {{
                var toRemove = [];
                for (var i = 0; i < origLS.length; i++) {{
                    var k = origLS.key(i);
                    if (k && k.startsWith(lsPrefix)) toRemove.push(k);
                }}
                toRemove.forEach(function(k) {{ origLS.removeItem(k); }});
            }},
            get length() {{
                var count = 0;
                for (var i = 0; i < origLS.length; i++) {{
                    if (origLS.key(i) && origLS.key(i).startsWith(lsPrefix)) count++;
                }}
                return count;
            }},
            key: function(n) {{
                var count = 0;
                for (var i = 0; i < origLS.length; i++) {{
                    var k = origLS.key(i);
                    if (k && k.startsWith(lsPrefix)) {{
                        if (count === n) return k.substring(lsPrefix.length);
                        count++;
                    }}
                }}
                return null;
            }}
        }};
        try {{
            Object.defineProperty(window, 'localStorage', {{
                get: function() {{ return proxyStorage; }},
                configurable: true,
            }});
        }} catch(e) {{}}

        // Pre-populate auth tokens with the real token from the proxy.
        // This prevents the HA frontend from showing the login screen.
        var proxyToken = "{token}";
        if (proxyToken) {{
            proxyStorage.setItem("hassTokens", JSON.stringify({{
                "access_token": proxyToken,
                "token_type": "Bearer",
                "refresh_token": "",
                "expires_in": 31536000,
                "hassUrl": location.origin + P,
                "clientId": location.origin + "/",
                "expires": Date.now() + 31536000000
            }}));
        }}

        // Override WebSocket
        var OrigWS = window.WebSocket;
        window.WebSocket = function(url, protocols) {{
            if (url) {{
                var u = new URL(url, location.href);
                if (!u.pathname.startsWith(P)) {{
                    u.pathname = P + u.pathname;
                }}
                url = u.toString();
            }}
            return protocols ? new OrigWS(url, protocols) : new OrigWS(url);
        }};
        window.WebSocket.prototype = OrigWS.prototype;
        window.WebSocket.CONNECTING = OrigWS.CONNECTING;
        window.WebSocket.OPEN = OrigWS.OPEN;
        window.WebSocket.CLOSING = OrigWS.CLOSING;
        window.WebSocket.CLOSED = OrigWS.CLOSED;

        // Override fetch
        var origFetch = window.fetch;
        window.fetch = function(input, init) {{
            if (typeof input === 'string' && input.startsWith('/') && !input.startsWith(P)) {{
                input = P + input;
            }}
            return origFetch.call(this, input, init);
        }};

        // Override XMLHttpRequest
        var origXHROpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {{
            if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(P)) {{
                url = P + url;
            }}
            return origXHROpen.apply(this, [method, url].concat(Array.prototype.slice.call(arguments, 2)));
        }};

        // Set the clean dashboard path so the HA router navigates correctly.
        // The HA frontend reads location.pathname on init to determine the panel.
        var origReplace = history.replaceState;
        var targetPath = location.pathname;
        if (targetPath.startsWith(P)) {{
            var cleanPath = targetPath.substring(P.length) || "/";
            origReplace.call(history, null, "", cleanPath + location.search);
        }}
    }})();
    </script>""".encode()

    body = body.replace(b"<head>", b"<head>" + proxy_script, 1)
    if b"<head>" not in body:
        body = _HEAD_TAG_RE.sub(lambda m: m.group(0) + proxy_script, body, count=1)

    return body


def _unused_rewrite_html(body: bytes, instance_id: str, original_path: str) -> bytes:
    """Rewrite absolute paths in HTML to go through the proxy."""
    prefix = f"{PROXY_PATH}/{instance_id}".encode()
    proxy_prefix = f"{PROXY_PATH}/{instance_id}"

    # Rewrite absolute-path HTML attributes (src, href, etc.)
    body = _ABS_ATTR_RE.sub(lambda m: m.group(1) + prefix + m.group(2), body)

    # Also rewrite /manifest.json which is at root level
    body = body.replace(
        b'href="/manifest.json"',
        f'href="{proxy_prefix}/manifest.json"'.encode(),
    )

    # Inject comprehensive proxy fix script
    # This overrides WebSocket, fetch, and history APIs so the HA frontend
    # routes all traffic through our proxy instead of the local instance.
    proxy_fix = f"""<script>
    (function() {{
        var P = "{proxy_prefix}";

        // Override WebSocket to redirect /api/websocket to our proxy
        var OrigWS = window.WebSocket;
        window.WebSocket = function(url, protocols) {{
            if (url && (url.indexOf("/api/websocket") !== -1)) {{
                // Rewrite ws://host/api/websocket to ws://host/P/api/websocket
                var u = new URL(url, location.href);
                if (!u.pathname.startsWith(P)) {{
                    u.pathname = P + u.pathname;
                }}
                url = u.toString();
            }}
            return protocols ? new OrigWS(url, protocols) : new OrigWS(url);
        }};
        window.WebSocket.prototype = OrigWS.prototype;
        window.WebSocket.CONNECTING = OrigWS.CONNECTING;
        window.WebSocket.OPEN = OrigWS.OPEN;
        window.WebSocket.CLOSING = OrigWS.CLOSING;
        window.WebSocket.CLOSED = OrigWS.CLOSED;

        // Override fetch to redirect absolute API calls through proxy
        var origFetch = window.fetch;
        window.fetch = function(input, init) {{
            if (typeof input === 'string' && input.startsWith('/') && !input.startsWith(P)) {{
                input = P + input;
            }} else if (input instanceof Request && input.url.startsWith(location.origin + '/')) {{
                var path = input.url.substring(location.origin.length);
                if (!path.startsWith(P)) {{
                    input = new Request(location.origin + P + path, input);
                }}
            }}
            return origFetch.call(this, input, init);
        }};

        // Override XMLHttpRequest to redirect through proxy
        var origXHROpen = XMLHttpRequest.prototype.open;
        XMLHttpRequest.prototype.open = function(method, url) {{
            if (typeof url === 'string' && url.startsWith('/') && !url.startsWith(P)) {{
                url = P + url;
            }}
            return origXHROpen.apply(this, [method, url, ...Array.prototype.slice.call(arguments, 2)]);
        }};

        // Set the clean dashboard path and protect it from being overwritten
        // by HA's router during initialization.
        var origPush = history.pushState;
        var origReplace = history.replaceState;
        var targetDashPath = "";
        var dashLoaded = false;

        var targetPath = location.pathname;
        if (targetPath.startsWith(P)) {{
            targetDashPath = targetPath.substring(P.length) || "/";
            // Set initial clean path
            origReplace.call(history, null, "", targetDashPath + location.search);

            // Temporarily block HA from changing the path during init
            // (HA's router calls replaceState with /lovelace/0 on startup)
            history.replaceState = function(state, title, url) {{
                if (!dashLoaded && typeof url === 'string') {{
                    // Keep our target path during initialization
                    return origReplace.call(this, state, title, targetDashPath + location.search);
                }}
                return origReplace.call(this, state, title, url);
            }};
            history.pushState = function(state, title, url) {{
                if (!dashLoaded && typeof url === 'string') {{
                    return origPush.call(this, state, title, targetDashPath + location.search);
                }}
                return origPush.call(this, state, title, url);
            }};

            // Stop blocking after the dashboard has loaded
            setTimeout(function() {{ dashLoaded = true; }}, 5000);
        }}
    }})();
    </script>""".encode()

    # Inject as FIRST thing after <head> so it runs before any HA scripts
    body = body.replace(b"<head>", b"<head>" + proxy_fix, 1)
    # Also handle <head ...> with attributes
    if b"<head>" not in body:
        body = _HEAD_TAG_RE.sub(lambda m: m.group(0) + proxy_fix, body, count=1)

    return body


def _unused_rewrite_js(body: bytes, instance_id: str) -> bytes:
    """Rewrite absolute paths in JS to go through the proxy."""
    prefix = f"{PROXY_PATH}/{instance_id}".encode()
    return _URL_IN_SCRIPT_RE.sub(lambda m: m.group(1) + prefix + m.group(2), body)


class DashboardProxyHTTPView(HomeAssistantView):
    """Proxy HTTP requests to a remote HA instance."""

    url = f"{PROXY_PATH}/{{instance_id}}/{{path:.*}}"
    name = "api:shared_ha:proxy"
    requires_auth = False  # Auth handled by remote token; local access via sidebar

    def __init__(self, proxy: DashboardProxy) -> None:
        """Initialize the proxy view."""
        self._proxy = proxy

    async def _handle(
        self, request: web.Request, instance_id: str, path: str
    ) -> web.StreamResponse:
        """Handle proxy requests."""
        info = self._proxy.get_remote_info(instance_id)
        if not info:
            return web.Response(status=404, text="Unknown instance")

        remote_url = info["url"].rstrip("/")
        token = info.get("token", "")

        # Create/reuse a session for proxying
        session = info.get("session")
        if not session or session.closed:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=60, connect=10),
            )
            info["session"] = session

        # Proxy all requests — solves mixed content (HTTPS→HTTP) and auth
        return await self._proxy_request(
            request, instance_id, remote_url, token, session, path
        )

    def _serve_iframe_wrapper(
        self, remote_url: str, path: str, query_string: str
    ) -> web.Response:
        """Serve a wrapper that iframes the remote HA with auto-auth."""
        dashboard_url = f"{remote_url}/{path}"
        if query_string:
            dashboard_url += f"?{query_string}"

        # The HA frontend stores auth in localStorage with key "hassTokens"
        # keyed by the origin URL. We inject a script that stores the token
        # in an iframe pointing to the remote origin, then loads the dashboard.
        # But since we can't access cross-origin localStorage from here,
        # we use a two-step approach:
        # 1. First iframe loads a proxy page that sets the token
        # 2. Then redirects to the actual dashboard

        # Actually simpler: the HA frontend accepts auth via the WS connection.
        # If the user is not logged in, the frontend shows login page.
        # But the frontend also checks for ?auth_callback=1&code=xxx parameters.
        # We can use the OAuth flow to auto-authenticate.
        #
        # Simplest: just iframe the remote URL directly. If the user has
        # already logged in to the Haus instance in this browser, the
        # session cookie persists and the dashboard loads.
        # If not, they see a login page — they log in once and it works
        # from then on.

        wrapper = f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<style>html,body{{margin:0;padding:0;height:100%;overflow:hidden}}iframe{{width:100%;height:100%;border:none}}</style>
</head><body>
<iframe src="{dashboard_url}" allow="fullscreen"></iframe>
</body></html>"""

        return web.Response(status=200, text=wrapper, content_type="text/html")

    async def _serve_dashboard_wrapper(
        self,
        request: web.Request,
        instance_id: str,
        remote_url: str,
        token: str,
        session: aiohttp.ClientSession,
        path: str,
    ) -> web.Response:
        """Serve a wrapper page that iframes the remote dashboard with a signed URL."""
        dashboard_path = f"/{path}"
        if request.query_string:
            dashboard_path += f"?{request.query_string}"

        # Get a signed URL from the remote
        signed_path = await self._get_signed_url(
            remote_url, token, session, dashboard_path
        )
        if not signed_path:
            return web.Response(
                status=502, text="Failed to get signed URL from remote instance"
            )

        sign_api = f"{PROXY_PATH}/{instance_id}/_sign_url"
        full_signed_url = f"{remote_url}{signed_path}"

        wrapper_html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Shared Dashboard</title>
    <style>
        html, body {{ margin: 0; padding: 0; height: 100%; overflow: hidden; }}
        iframe {{ width: 100%; height: 100%; border: none; }}
    </style>
</head>
<body>
    <iframe id="dashboard" src="{full_signed_url}" allow="fullscreen"></iframe>
    <script>
        // Refresh signed URL every 4 minutes (expires in 5)
        setInterval(async function() {{
            try {{
                var resp = await fetch("{sign_api}?path={dashboard_path}");
                var data = await resp.json();
                if (data.signed_url) {{
                    document.getElementById("dashboard").src = data.signed_url;
                }}
            }} catch(e) {{
                console.warn("Failed to refresh signed URL:", e);
            }}
        }}, 240000);
    </script>
</body>
</html>"""

        return web.Response(
            status=200, text=wrapper_html, content_type="text/html"
        )

    async def _handle_sign_url(
        self,
        request: web.Request,
        instance_id: str,
        remote_url: str,
        token: str,
        session: aiohttp.ClientSession,
    ) -> web.Response:
        """Return a fresh signed URL for the dashboard."""
        dashboard_path = request.query.get("path", "/lovelace")
        signed_path = await self._get_signed_url(
            remote_url, token, session, dashboard_path
        )
        if signed_path:
            return web.json_response(
                {"signed_url": f"{remote_url}{signed_path}"}
            )
        return web.json_response({"error": "Failed to sign URL"}, status=502)

    async def _get_signed_url(
        self,
        remote_url: str,
        token: str,
        session: aiohttp.ClientSession,
        path: str,
    ) -> str | None:
        """Get a signed URL from the remote HA instance via WebSocket."""
        ws_url = remote_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        ) + "/api/websocket"

        try:
            remote_ws = await session.ws_connect(ws_url, heartbeat=30)

            # Auth handshake
            auth_msg = await remote_ws.receive_json()
            if auth_msg.get("type") == "auth_required":
                await remote_ws.send_json(
                    {"type": "auth", "access_token": token}
                )
                auth_result = await remote_ws.receive_json()
                if auth_result.get("type") != "auth_ok":
                    await remote_ws.close()
                    return None

            # Request signed path
            await remote_ws.send_json({
                "id": 1,
                "type": "auth/sign_path",
                "path": path,
                "expires": 300,
            })
            result = await remote_ws.receive_json()
            await remote_ws.close()

            if result.get("success"):
                return result["result"]["path"]
            return None
        except Exception:
            _LOGGER.warning("Failed to get signed URL from %s", remote_url)
            return None

    async def _proxy_request(
        self,
        request: web.Request,
        instance_id: str,
        remote_url: str,
        token: str,
        session: aiohttp.ClientSession,
        path: str,
    ) -> web.StreamResponse:
        """Proxy an HTTP request to the remote instance."""
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

                if resp.status == 304:
                    return web.Response(status=304, headers=resp_headers)

                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location", "")
                    if location.startswith("/"):
                        resp_headers["Location"] = (
                            f"{PROXY_PATH}/{instance_id}{location}"
                        )
                    return web.Response(status=resp.status, headers=resp_headers)

                # For HTML, rewrite paths and inject proxy scripts
                if "text/html" in content_type:
                    raw = await resp.read()
                    if len(raw) <= _MAX_REWRITE_SIZE:
                        raw = _rewrite_html_proxy(raw, instance_id, path, token)
                    return web.Response(
                        status=resp.status,
                        headers=resp_headers,
                        body=raw,
                        content_type="text/html",
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
    requires_auth = False  # Auth handled by remote token

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
        token = info.get("token", "")
        session = info.get("session")
        if not session or session.closed:
            session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False),
                timeout=aiohttp.ClientTimeout(total=60, connect=10),
            )
            info["session"] = session

        # Convert http(s) to ws(s)
        ws_url = remote_url.replace("https://", "wss://").replace(
            "http://", "ws://"
        )
        ws_url += "/api/websocket"

        local_ws = web.WebSocketResponse(heartbeat=30)
        await local_ws.prepare(request)

        try:
            remote_ws = await session.ws_connect(ws_url, heartbeat=30)

            # HA WS auth flow:
            # 1. Remote sends auth_required → forward to browser
            # 2. Browser sends auth (with local token) → intercept, replace with remote token
            # 3. Remote sends auth_ok → forward to browser
            auth_msg = await remote_ws.receive_json()
            if auth_msg.get("type") == "auth_required":
                # Forward auth_required to browser
                await local_ws.send_json(auth_msg)

                # Wait for browser's auth message (it will send a local token)
                browser_auth = await local_ws.receive_json()

                # Replace with our remote token
                await remote_ws.send_json(
                    {"type": "auth", "access_token": token}
                )

                # Wait for remote's auth result
                auth_result = await remote_ws.receive_json()
                if auth_result.get("type") != "auth_ok":
                    await local_ws.send_json(
                        {"type": "auth_invalid", "message": "Remote auth failed"}
                    )
                    await local_ws.close()
                    return local_ws

                # Forward auth_ok to browser
                await local_ws.send_json(auth_result)

            # Bidirectional proxy with path rewriting
            proxy_prefix = f"{PROXY_PATH}/{instance_id}"

            async def forward_browser_to_remote(src, dst):
                """Forward messages from browser to remote (no rewriting)."""
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        await dst.send_str(msg.data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            async def forward_remote_to_browser(src, dst):
                """Forward messages from remote to browser, rewriting resource URLs."""
                async for msg in src:
                    if msg.type == WSMsgType.TEXT:
                        data = msg.data
                        # Rewrite /local/ and /hacsfiles/ paths in WS responses
                        # so the browser loads custom card JS through the proxy
                        if "/local/" in data or "/hacsfiles/" in data:
                            data = data.replace(
                                '"/local/', f'"{proxy_prefix}/local/'
                            ).replace(
                                '"/hacsfiles/', f'"{proxy_prefix}/hacsfiles/'
                            )
                        await dst.send_str(data)
                    elif msg.type == WSMsgType.BINARY:
                        await dst.send_bytes(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            await asyncio.gather(
                forward_browser_to_remote(local_ws, remote_ws),
                forward_remote_to_browser(remote_ws, local_ws),
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
