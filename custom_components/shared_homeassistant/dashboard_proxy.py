"""Dashboard sharing for Shared Home Assistant.

Discovers shared dashboards from other instances via MQTT and
registers them as sidebar panels (direct iframe to source instance).
No tokens are transmitted — users authenticate directly with the source.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    TOPIC_DASHBOARD_INFO,
    TOPIC_SUB_DASHBOARD_INFO,
    CONF_INSTANCE_URL,
    CONF_SHARE_DASHBOARDS,
    CONF_SHARED_DASHBOARD_LIST,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)


class DashboardProxy:
    """Manages dashboard sharing: discovery via MQTT and sidebar registration."""

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

        # Remote instance data: {instance_id: {url, instance_name, dashboards}}
        self._remote_instances: dict[str, dict[str, Any]] = {}

    async def async_update_config(self, config: dict[str, Any]) -> None:
        """Update config and re-publish dashboard info if needed."""
        self._instance_url = config.get(CONF_INSTANCE_URL, "")
        self._share_dashboards = config.get(CONF_SHARE_DASHBOARDS, False)
        self._shared_dashboard_list = config.get(CONF_SHARED_DASHBOARD_LIST, [])

        if self._share_dashboards and self._instance_url:
            await self._publish_dashboard_info()
        else:
            topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
            await self._mqtt.async_publish(topic, "", retain=True)

    async def async_register_subscriptions(self) -> None:
        """Pre-register MQTT subscriptions (before connect)."""
        await self._mqtt.async_subscribe(
            TOPIC_SUB_DASHBOARD_INFO, self._handle_dashboard_info
        )

    async def async_start(self) -> None:
        """Start dashboard sharing (subscriptions already registered)."""
        if self._share_dashboards and self._instance_url:
            await self._publish_dashboard_info()

        # Re-publish dashboard info on every MQTT reconnect to recover from
        # retained-message loss (broker restart / VM pause).
        self._mqtt.add_reconnect_callback(self._republish_on_reconnect)

    async def _republish_on_reconnect(self) -> None:
        """Re-publish dashboard info after an MQTT reconnect."""
        if self._share_dashboards and self._instance_url:
            await self._publish_dashboard_info()

    async def async_stop(self) -> None:
        """Stop dashboard sharing."""
        self._mqtt.remove_reconnect_callback(self._republish_on_reconnect)
        await self._mqtt.async_unsubscribe(TOPIC_SUB_DASHBOARD_INFO)

        if self._share_dashboards:
            topic = TOPIC_DASHBOARD_INFO.format(instance_id=self._instance_id)
            await self._mqtt.async_publish(topic, "", retain=True)

    async def _publish_dashboard_info(self) -> None:
        """Publish available dashboards to MQTT for discovery."""
        dashboards = await self._get_dashboard_list()

        payload = {
            "instance_id": self._instance_id,
            "instance_name": self._instance_name,
            "url": self._instance_url,
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
        remote_url = info.get("url", "").rstrip("/")
        if not remote_url:
            return

        for dashboard in info.get("dashboards", []):
            url_path = dashboard.get("url_path", "")
            title = dashboard.get("title", url_path)
            icon = dashboard.get("icon", "mdi:view-dashboard")

            # Default dashboard: use instance name as sidebar title
            if url_path == "lovelace" and instance_name:
                sidebar_title = instance_name.title()
            else:
                sidebar_title = title

            panel_url_path = f"shared-{instance_id[:8]}-{url_path}"

            # Direct iframe to the remote HA's dashboard URL
            if url_path == "lovelace":
                dashboard_url = f"{remote_url}/lovelace/0?kiosk"
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
                sidebar_title,
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
        """Get remote instance connection info."""
        return self._remote_instances.get(instance_id)
