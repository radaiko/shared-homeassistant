"""Publisher for Shared Home Assistant v2 — emits MQTT Discovery.

Publishes shared entities to the peer's broker so the peer's native MQTT
integration picks them up via Discovery. Subscribes to command topics on
the peer broker and routes inbound commands back to local HA services.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    CONF_INSTANCE_ID,
    CONF_INSTANCE_NAME,
    CONF_PEER_DISCOVERY_PREFIX,
    CONF_READONLY_DEVICES,
    CONF_READONLY_ENTITIES,
    CONF_READONLY_INTEGRATIONS,
    CONF_SHARED_DEVICES,
    CONF_SHARED_ENTITIES,
    CONF_SHARED_INTEGRATIONS,
    DEFAULT_DISCOVERY_PREFIX,
    PAYLOAD_OFFLINE,
    PAYLOAD_ONLINE,
    SETTABLE_DOMAINS,
    SUPPORTED_DOMAINS,
    TOPIC_BRIDGE_AVAILABILITY,
    TOPIC_DISCOVERY_CONFIG,
    TOPIC_ENTITY_ATTR,
    TOPIC_ENTITY_SET,
    TOPIC_ENTITY_SET_WILDCARD,
    TOPIC_ENTITY_STATE,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)

_OBJECT_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_object_id(raw: str) -> str:
    """Coerce a string into a valid MQTT Discovery object_id.

    HA's discovery topic matcher only accepts [a-zA-Z0-9_-]+ in the
    object_id slot. Unique IDs from other integrations often contain
    dots, colons, or spaces, which HA silently rejects.
    """
    safe = _OBJECT_ID_SAFE.sub("_", raw)
    return safe.strip("_") or "entity"


SKIP_ATTRIBUTES = {
    "friendly_name",
    "supported_features",
    "supported_color_modes",
    "entity_picture",
    "icon",
    "assumed_state",
    "attribution",
    "device_class",
    "state_class",
    "restored",
}


class Publisher:
    """Publishes local entities to a peer HA via MQTT Discovery."""

    def __init__(
        self,
        hass: HomeAssistant,
        peer_mqtt: MQTTClient,
        config: dict[str, Any],
    ) -> None:
        self._hass = hass
        self._mqtt = peer_mqtt
        self._instance_id: str = config[CONF_INSTANCE_ID]
        self._instance_name: str = config[CONF_INSTANCE_NAME]
        self._discovery_prefix: str = config.get(
            CONF_PEER_DISCOVERY_PREFIX, DEFAULT_DISCOVERY_PREFIX
        )

        self._config = dict(config)

        # Tracks the full set of entities we have published discovery for, so
        # we can clean them up on unshare/stop.
        self._published_entities: dict[str, dict[str, Any]] = {}
        self._unsub_state_listener: Any = None
        self._bridge_unique_id = f"shared_ha_bridge_{self._instance_id}"

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def async_start(self) -> None:
        """Publish bridge + shared entities, subscribe to commands."""
        # Subscribe to inbound command topic (on peer broker)
        set_wildcard = TOPIC_ENTITY_SET_WILDCARD.format(instance_id=self._instance_id)
        await self._mqtt.async_subscribe(set_wildcard, self._handle_command)

        await self._publish_bridge()
        await self._publish_all_shared()

        # State listener for live updates
        self._unsub_state_listener = self._hass.bus.async_listen(
            "state_changed", self._handle_state_changed
        )

        # Republish everything after each MQTT reconnect — self-heals after
        # broker restarts or retained-message loss.
        self._mqtt.add_reconnect_callback(self._republish_retained)

    async def async_stop(self) -> None:
        """Remove discovery configs + announce offline."""
        self._mqtt.remove_reconnect_callback(self._republish_retained)

        if self._unsub_state_listener:
            self._unsub_state_listener()
            self._unsub_state_listener = None

        set_wildcard = TOPIC_ENTITY_SET_WILDCARD.format(instance_id=self._instance_id)
        await self._mqtt.async_unsubscribe(set_wildcard)

        # Remove per-entity discovery configs (empty retained payload)
        for entity_id, info in list(self._published_entities.items()):
            await self._unpublish_entity(entity_id, info)

        # Remove bridge discovery + publish offline
        await self._unpublish_bridge()
        await self._mqtt.async_publish(
            TOPIC_BRIDGE_AVAILABILITY.format(instance_id=self._instance_id),
            PAYLOAD_OFFLINE,
            retain=True,
        )

    async def async_update_selection(self, config: dict[str, Any]) -> None:
        """Reconcile published entities with new selection."""
        self._config = dict(config)

        old_ids = set(self._published_entities)
        new_map = self._resolve_selection()
        new_ids = set(new_map)

        # Removed
        for entity_id in old_ids - new_ids:
            info = self._published_entities.pop(entity_id, None)
            if info:
                await self._unpublish_entity(entity_id, info)

        # Added or changed: (re)publish
        for entity_id in new_ids:
            meta = new_map[entity_id]
            await self._publish_entity(entity_id, meta)

    # ------------------------------------------------------------------
    # reconnect callback
    # ------------------------------------------------------------------

    async def _republish_retained(self) -> None:
        """Called on every MQTT reconnect — refresh broker-side retained state."""
        await self._publish_bridge()
        await self._publish_all_shared(refresh=True)

    # ------------------------------------------------------------------
    # bridge (via_device anchor)
    # ------------------------------------------------------------------

    async def _publish_bridge(self) -> None:
        """Publish the bridge-connectivity binary_sensor discovery."""
        avail_topic = TOPIC_BRIDGE_AVAILABILITY.format(instance_id=self._instance_id)

        config = {
            "name": "Connectivity",
            "unique_id": self._bridge_unique_id,
            "state_topic": avail_topic,
            "payload_on": PAYLOAD_ONLINE,
            "payload_off": PAYLOAD_OFFLINE,
            "device_class": "connectivity",
            "device": {
                "identifiers": [self._bridge_unique_id],
                "name": f"{self._instance_name} (Shared HA)",
                "manufacturer": "Shared Home Assistant",
                "model": "Bridge",
                "sw_version": "2.0",
            },
        }

        topic = TOPIC_DISCOVERY_CONFIG.format(
            discovery_prefix=self._discovery_prefix,
            component="binary_sensor",
            object_id=self._bridge_unique_id,
        )
        await self._mqtt.async_publish(topic, json.dumps(config), retain=True)

        # Mark bridge online
        await self._mqtt.async_publish(avail_topic, PAYLOAD_ONLINE, retain=True)

    async def _unpublish_bridge(self) -> None:
        topic = TOPIC_DISCOVERY_CONFIG.format(
            discovery_prefix=self._discovery_prefix,
            component="binary_sensor",
            object_id=self._bridge_unique_id,
        )
        await self._mqtt.async_publish(topic, "", retain=True)

    # ------------------------------------------------------------------
    # selection resolution
    # ------------------------------------------------------------------

    def _resolve_selection(self) -> dict[str, dict[str, Any]]:
        """Resolve config selections into the effective shared entity map.

        Returns {entity_id: {"readonly": bool}} for every entity we intend
        to publish.
        """
        ent_reg = er.async_get(self._hass)
        dev_reg = dr.async_get(self._hass)

        shared_integrations = set(self._config.get(CONF_SHARED_INTEGRATIONS, []))
        shared_devices = set(self._config.get(CONF_SHARED_DEVICES, []))
        shared_entities = set(self._config.get(CONF_SHARED_ENTITIES, []))
        readonly_integrations = set(
            self._config.get(CONF_READONLY_INTEGRATIONS, [])
        )
        readonly_devices = set(self._config.get(CONF_READONLY_DEVICES, []))
        readonly_entities = set(self._config.get(CONF_READONLY_ENTITIES, []))

        result: dict[str, dict[str, Any]] = {}

        def _include(entity_id: str, readonly: bool) -> None:
            entry = ent_reg.async_get(entity_id)
            if entry is None or entry.disabled:
                return
            if entry.domain not in SUPPORTED_DOMAINS:
                return
            # First include wins; keep strictest (readonly) if already readonly
            if entity_id in result:
                result[entity_id]["readonly"] = (
                    result[entity_id]["readonly"] or readonly
                )
            else:
                result[entity_id] = {"readonly": readonly}

        # Integration-level sharing → walk every device in each entry
        for entry_id in shared_integrations:
            ro_integration = entry_id in readonly_integrations
            for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
                if device.id in shared_devices:
                    continue  # handled by device loop with possibly different flag
                for entry in er.async_entries_for_device(ent_reg, device.id):
                    _include(entry.entity_id, ro_integration)

        # Device-level sharing
        for device_id in shared_devices:
            ro_device = device_id in readonly_devices
            for entry in er.async_entries_for_device(ent_reg, device_id):
                _include(entry.entity_id, ro_device)

        # Entity-level sharing
        for entity_id in shared_entities:
            ro_entity = entity_id in readonly_entities
            _include(entity_id, ro_entity)

        return result

    # ------------------------------------------------------------------
    # per-entity publishing
    # ------------------------------------------------------------------

    async def _publish_all_shared(self, refresh: bool = False) -> None:
        """Publish discovery + state for every shared entity."""
        entities = self._resolve_selection()
        if not refresh:
            # On initial start, reset tracking to match reality
            self._published_entities = {}

        _LOGGER.info(
            "Publishing %d shared entities to peer broker (discovery prefix: %s)",
            len(entities),
            self._discovery_prefix,
        )

        for entity_id, meta in entities.items():
            try:
                await self._publish_entity(entity_id, meta)
            except Exception:
                _LOGGER.exception("Failed to publish %s", entity_id)

    async def _publish_entity(self, entity_id: str, meta: dict[str, Any]) -> None:
        """Publish discovery + current state for a single entity."""
        ent_reg = er.async_get(self._hass)
        dev_reg = dr.async_get(self._hass)

        entry = ent_reg.async_get(entity_id)
        if entry is None or entry.domain not in SUPPORTED_DOMAINS:
            return

        device = dev_reg.async_get(entry.device_id) if entry.device_id else None
        state = self._hass.states.get(entity_id)
        readonly = meta.get("readonly", False)

        config = self._build_discovery_config(
            entity_id=entity_id,
            entry=entry,
            device=device,
            state=state,
            readonly=readonly,
        )
        if config is None:
            return

        component = entry.domain
        object_id = config["unique_id"]

        discovery_topic = TOPIC_DISCOVERY_CONFIG.format(
            discovery_prefix=self._discovery_prefix,
            component=component,
            object_id=object_id,
        )
        await self._mqtt.async_publish(
            discovery_topic, json.dumps(config), retain=True
        )
        _LOGGER.debug(
            "Published discovery for %s → %s", entity_id, discovery_topic
        )

        # Publish current state + attrs
        if state is not None:
            await self._publish_state(entity_id, state, entry.domain)

        self._published_entities[entity_id] = {
            "component": component,
            "object_id": object_id,
            "readonly": readonly,
        }

    async def _unpublish_entity(self, entity_id: str, info: dict[str, Any]) -> None:
        """Remove discovery + state topics for an entity."""
        discovery_topic = TOPIC_DISCOVERY_CONFIG.format(
            discovery_prefix=self._discovery_prefix,
            component=info["component"],
            object_id=info["object_id"],
        )
        await self._mqtt.async_publish(discovery_topic, "", retain=True)

        for topic_tpl in (TOPIC_ENTITY_STATE, TOPIC_ENTITY_ATTR):
            await self._mqtt.async_publish(
                topic_tpl.format(instance_id=self._instance_id, entity_id=entity_id),
                "",
                retain=True,
            )

    def _build_discovery_config(
        self,
        entity_id: str,
        entry: er.RegistryEntry,
        device: dr.DeviceEntry | None,
        state: State | None,
        readonly: bool,
    ) -> dict[str, Any] | None:
        """Build the MQTT Discovery payload for one entity."""
        domain = entry.domain

        raw_uid = entry.unique_id or entity_id
        unique_id = _safe_object_id(
            f"shared_ha_{self._instance_id}_{raw_uid}"
        )
        state_topic = TOPIC_ENTITY_STATE.format(
            instance_id=self._instance_id, entity_id=entity_id
        )
        attr_topic = TOPIC_ENTITY_ATTR.format(
            instance_id=self._instance_id, entity_id=entity_id
        )
        avail_topic = TOPIC_BRIDGE_AVAILABILITY.format(instance_id=self._instance_id)

        # Friendly name
        name = entry.name or entry.original_name
        if not name and state is not None:
            name = state.attributes.get("friendly_name")
        if not name:
            name = entity_id.split(".", 1)[-1].replace("_", " ").title()

        config: dict[str, Any] = {
            "name": name,
            "unique_id": unique_id,
            "object_id": entity_id.split(".", 1)[-1],
            "state_topic": state_topic,
            "json_attributes_topic": attr_topic,
            "availability_topic": avail_topic,
            "payload_available": PAYLOAD_ONLINE,
            "payload_not_available": PAYLOAD_OFFLINE,
        }

        # Device block
        if device is not None:
            device_block: dict[str, Any] = {
                "identifiers": [f"shared_ha_{self._instance_id}_{device.id}"],
                "name": device.name_by_user or device.name or "Unknown",
                "via_device": self._bridge_unique_id,
            }
            if device.manufacturer:
                device_block["manufacturer"] = device.manufacturer
            if device.model:
                device_block["model"] = device.model
            if device.sw_version:
                device_block["sw_version"] = device.sw_version
            if device.hw_version:
                device_block["hw_version"] = device.hw_version
            config["device"] = device_block
        else:
            # Orphan entity — still anchor to the bridge
            config["device"] = {
                "identifiers": [f"shared_ha_{self._instance_id}_orphan"],
                "name": f"{self._instance_name} shared entities",
                "via_device": self._bridge_unique_id,
            }

        # Carry over metadata if present
        dc = entry.device_class or entry.original_device_class
        if dc:
            config["device_class"] = dc
        if entry.unit_of_measurement:
            config["unit_of_measurement"] = entry.unit_of_measurement

        # Command topic + per-domain schema
        if not readonly and domain in SETTABLE_DOMAINS:
            set_topic = TOPIC_ENTITY_SET.format(
                instance_id=self._instance_id, entity_id=entity_id
            )
            config["command_topic"] = set_topic

        self._augment_domain_config(config, domain, state)

        return config

    @staticmethod
    def _augment_domain_config(
        config: dict[str, Any], domain: str, state: State | None
    ) -> None:
        """Fill in domain-specific discovery fields."""
        if domain == "switch" or domain == "input_boolean":
            config["payload_on"] = "ON"
            config["payload_off"] = "OFF"
            config["state_on"] = "on"
            config["state_off"] = "off"

        elif domain == "binary_sensor":
            config["payload_on"] = "on"
            config["payload_off"] = "off"

        elif domain == "cover":
            config["payload_open"] = "OPEN"
            config["payload_close"] = "CLOSE"
            config["payload_stop"] = "STOP"
            config["state_open"] = "open"
            config["state_closed"] = "closed"

        elif domain == "lock":
            config["payload_lock"] = "LOCK"
            config["payload_unlock"] = "UNLOCK"
            config["state_locked"] = "locked"
            config["state_unlocked"] = "unlocked"

        elif domain == "number" or domain == "input_number":
            if state is not None:
                if (mn := state.attributes.get("min")) is not None:
                    config["min"] = mn
                if (mx := state.attributes.get("max")) is not None:
                    config["max"] = mx
                if (step := state.attributes.get("step")) is not None:
                    config["step"] = step

        elif domain == "light":
            config["schema"] = "json"
            if state is not None:
                modes = state.attributes.get("supported_color_modes")
                if modes:
                    config["supported_color_modes"] = list(modes)
                if "brightness" in state.attributes:
                    config["brightness"] = True

    # ------------------------------------------------------------------
    # state publishing
    # ------------------------------------------------------------------

    async def _publish_state(
        self, entity_id: str, state: State, domain: str
    ) -> None:
        """Publish state + attrs to MQTT."""
        state_topic = TOPIC_ENTITY_STATE.format(
            instance_id=self._instance_id, entity_id=entity_id
        )
        attr_topic = TOPIC_ENTITY_ATTR.format(
            instance_id=self._instance_id, entity_id=entity_id
        )

        # Domain-specific payload formatting for special schemas
        if domain == "light":
            payload = self._format_light_state(state)
        else:
            payload = str(state.state)

        await self._mqtt.async_publish(state_topic, payload, retain=True)

        attrs = {
            k: _jsonable(v)
            for k, v in state.attributes.items()
            if k not in SKIP_ATTRIBUTES
        }
        await self._mqtt.async_publish(
            attr_topic, json.dumps(attrs), retain=True
        )

    @staticmethod
    def _format_light_state(state: State) -> str:
        """JSON schema state payload for lights."""
        payload: dict[str, Any] = {"state": "ON" if state.state == "on" else "OFF"}
        if (b := state.attributes.get("brightness")) is not None:
            payload["brightness"] = b
        if (ct := state.attributes.get("color_temp")) is not None:
            payload["color_temp"] = ct
        if (rgb := state.attributes.get("rgb_color")) is not None:
            payload["color"] = {"r": rgb[0], "g": rgb[1], "b": rgb[2]}
        return json.dumps(payload)

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id is None or entity_id not in self._published_entities:
            return
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        info = self._published_entities[entity_id]
        self._hass.async_create_task(
            self._publish_state(entity_id, new_state, info["component"])
        )

    # ------------------------------------------------------------------
    # inbound commands
    # ------------------------------------------------------------------

    async def _handle_command(self, topic: str, payload: bytes) -> None:
        """Route a command from the peer to a local HA service."""
        # Topic shape: shared_ha/<iid>/e/<entity_id>/set
        parts = topic.split("/")
        if len(parts) < 5 or parts[-1] != "set":
            return
        entity_id = parts[-2]

        info = self._published_entities.get(entity_id)
        if info is None or info.get("readonly"):
            _LOGGER.debug("Ignoring command for unshared/readonly %s", entity_id)
            return

        try:
            raw = payload.decode() if payload else ""
        except UnicodeDecodeError:
            return

        domain = info["component"]
        service, data = _command_to_service(domain, entity_id, raw)
        if service is None:
            _LOGGER.warning(
                "Could not map command '%s' on %s for entity %s",
                raw, domain, entity_id,
            )
            return

        svc_domain, svc_name = service.split(".", 1)
        try:
            await self._hass.services.async_call(
                svc_domain, svc_name, data, blocking=False
            )
        except Exception:
            _LOGGER.exception("Service call %s failed for %s", service, entity_id)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return str(value)
    return value


def _command_to_service(
    domain: str, entity_id: str, payload: str
) -> tuple[str | None, dict[str, Any]]:
    """Translate an MQTT command payload into an HA service call."""
    data: dict[str, Any] = {"entity_id": entity_id}
    p = payload.strip()

    if domain in ("switch", "input_boolean"):
        if p.upper() == "ON":
            return (
                "switch.turn_on" if domain == "switch" else "input_boolean.turn_on",
                data,
            )
        if p.upper() == "OFF":
            return (
                "switch.turn_off" if domain == "switch" else "input_boolean.turn_off",
                data,
            )

    elif domain == "cover":
        u = p.upper()
        if u == "OPEN":
            return "cover.open_cover", data
        if u == "CLOSE":
            return "cover.close_cover", data
        if u == "STOP":
            return "cover.stop_cover", data

    elif domain == "lock":
        u = p.upper()
        if u == "LOCK":
            return "lock.lock", data
        if u == "UNLOCK":
            return "lock.unlock", data

    elif domain in ("number", "input_number"):
        try:
            data["value"] = float(p)
        except ValueError:
            return None, {}
        return (
            "number.set_value" if domain == "number" else "input_number.set_value",
            data,
        )

    elif domain == "light":
        try:
            js = json.loads(p)
        except json.JSONDecodeError:
            # Fall back to plain ON/OFF
            if p.upper() == "OFF":
                return "light.turn_off", data
            if p.upper() == "ON":
                return "light.turn_on", data
            return None, {}

        state = str(js.get("state", "")).upper()
        if state == "OFF":
            return "light.turn_off", data
        if state == "ON":
            if "brightness" in js:
                data["brightness"] = int(js["brightness"])
            if "color_temp" in js:
                data["color_temp"] = int(js["color_temp"])
            if "color" in js and isinstance(js["color"], dict):
                c = js["color"]
                if {"r", "g", "b"} <= c.keys():
                    data["rgb_color"] = [int(c["r"]), int(c["g"]), int(c["b"])]
            return "light.turn_on", data

    return None, {}
