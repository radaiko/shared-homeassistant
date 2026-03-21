"""Subscriber for Shared Home Assistant - receives shared devices/entities from MQTT."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import (
    DOMAIN,
    TOPIC_PREFIX,
    TOPIC_SUB_DEVICES,
    TOPIC_SUB_STATES,
    CONF_INSTANCE_ID,
    CONF_ENTITY_PREFIX,
    DEFAULT_ENTITY_PREFIX,
    PLATFORMS,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)


class Subscriber:
    """Subscribes to shared devices/entities from other instances."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: MQTTClient,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the subscriber."""
        self._hass = hass
        self._mqtt = mqtt_client
        self._config_entry = config_entry
        self._instance_id: str = config_entry.data[CONF_INSTANCE_ID]
        self._entity_prefix: str = config_entry.data.get(
            CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX
        )

        # Track remote devices and entities
        # {instance_id: {device_id: device_payload}}
        self._remote_devices: dict[str, dict[str, Any]] = {}
        # {instance_id: {entity_id: entity_data}}
        self._remote_entities: dict[str, dict[str, Any]] = {}
        # Track instance online status
        self._instance_status: dict[str, bool] = {}

        # Callbacks for entity platforms to register
        # {domain: async_add_entities_callback}
        self._platform_callbacks: dict[str, Any] = {}
        # Track created entities {unique_id: entity_object}
        self._created_entities: dict[str, Any] = {}
        # Cache state messages for entities not yet created
        # {(instance_id, entity_id): {"state": ..., "attributes": ...}}
        self._pending_states: dict[tuple[str, str], dict[str, Any]] = {}
        # Track which entities we've already requested history for
        self._history_requested: set[tuple[str, str]] = set()

    def register_platform(self, domain: str, async_add_entities: Any) -> None:
        """Register a platform's async_add_entities callback."""
        self._platform_callbacks[domain] = async_add_entities
        _LOGGER.debug("Registered platform callback for %s", domain)

    async def async_start(self) -> None:
        """Start subscribing to shared devices and states."""
        await self._mqtt.async_subscribe(TOPIC_SUB_DEVICES, self._handle_device)
        await self._mqtt.async_subscribe(TOPIC_SUB_STATES, self._handle_state)

        # Subscribe to heartbeats
        heartbeat_topic = f"{TOPIC_PREFIX}/+/heartbeat"
        await self._mqtt.async_subscribe(heartbeat_topic, self._handle_heartbeat)

    async def async_stop(self) -> None:
        """Stop subscribing and clean up."""
        await self._mqtt.async_unsubscribe(TOPIC_SUB_DEVICES)
        await self._mqtt.async_unsubscribe(TOPIC_SUB_STATES)
        await self._mqtt.async_unsubscribe(f"{TOPIC_PREFIX}/+/heartbeat")

    def get_entities_for_domain(self, domain: str) -> list[dict[str, Any]]:
        """Get all known remote entities for a given domain and register them."""
        from .entity_factory import create_entity

        result = []
        for instance_id, devices in self._remote_devices.items():
            instance_name = ""
            for device_id, device_data in devices.items():
                instance_name = device_data.get("instance_name", instance_id[:8])
                for entity_data in device_data.get("entities", []):
                    if entity_data.get("domain") != domain:
                        continue

                    unique_id = f"shared_ha_{instance_id}_{entity_data.get('unique_id', entity_data.get('entity_id'))}"
                    if unique_id in self._created_entities:
                        continue

                    entity = create_entity(
                        domain=domain,
                        entity_data=entity_data,
                        instance_id=instance_id,
                        instance_name=instance_name,
                        device_id=device_id,
                        mqtt_client=self._mqtt,
                        entity_prefix=self._entity_prefix,
                    )
                    if entity:
                        self._created_entities[unique_id] = entity
                        result.append(entity)

                        # Apply any pending state
                        remote_eid = entity_data.get("entity_id")
                        pending_key = (instance_id, remote_eid)
                        if pending_key in self._pending_states:
                            pending = self._pending_states.pop(pending_key)
                            entity.update_state(
                                state=pending["state"],
                                attributes=pending["attributes"],
                            )
        return result

    async def _handle_device(self, topic: str, payload: bytes) -> None:
        """Handle incoming device messages."""
        # Parse topic: shared_ha/{instance_id}/devices/{device_id}
        parts = topic.split("/")
        if len(parts) < 4:
            return

        instance_id = parts[1]

        # Skip our own messages
        if instance_id == self._instance_id:
            return

        device_id = "/".join(parts[3:])

        # Empty payload = device removed
        if not payload:
            await self._remove_device(instance_id, device_id)
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("Invalid device payload on topic %s", topic)
            return

        # Store remote device data
        if instance_id not in self._remote_devices:
            self._remote_devices[instance_id] = {}
        self._remote_devices[instance_id][device_id] = data

        # Create/update device in local registry
        dev_reg = dr.async_get(self._hass)

        identifiers = set()
        for ident in data.get("identifiers", []):
            if isinstance(ident, list) and len(ident) == 2:
                identifiers.add((f"shared_ha_{instance_id}", f"{ident[0]}_{ident[1]}"))

        # Always add our own identifier
        identifiers.add((DOMAIN, f"{instance_id}_{device_id}"))

        instance_name = data.get("instance_name", instance_id[:8])

        dev_reg.async_get_or_create(
            config_entry_id=self._config_entry.entry_id,
            identifiers=identifiers,
            name=f"{instance_name} - {data.get('name', 'Unknown')}",
            manufacturer=data.get("manufacturer"),
            model=data.get("model"),
            sw_version=data.get("sw_version"),
            hw_version=data.get("hw_version"),
        )

        # Create entities for each entity in the device payload
        for entity_data in data.get("entities", []):
            domain = entity_data.get("domain")
            if domain not in PLATFORMS:
                _LOGGER.debug(
                    "Unsupported domain %s for entity %s, skipping",
                    domain,
                    entity_data.get("entity_id"),
                )
                continue

            unique_id = f"shared_ha_{instance_id}_{entity_data.get('unique_id', entity_data.get('entity_id'))}"

            remote_eid = entity_data.get("entity_id")

            # Request history if not already done for this entity
            if remote_eid:
                history_key = (instance_id, remote_eid)
                if history_key not in self._history_requested:
                    self._history_requested.add(history_key)
                    # Stagger requests to avoid overwhelming MQTT
                    delay = len(self._history_requested) * 0.5
                    self._hass.async_create_task(
                        self._request_entity_history(
                            instance_id, remote_eid, delay
                        )
                    )

            if unique_id in self._created_entities:
                # Entity already exists, update it
                entity = self._created_entities[unique_id]
                entity.update_from_payload(entity_data)
                continue

            # Create new entity via platform callback
            if domain in self._platform_callbacks:
                from .entity_factory import create_entity

                entity = create_entity(
                    domain=domain,
                    entity_data=entity_data,
                    instance_id=instance_id,
                    instance_name=instance_name,
                    device_id=device_id,
                    mqtt_client=self._mqtt,
                    entity_prefix=self._entity_prefix,
                )
                if entity:
                    self._created_entities[unique_id] = entity
                    self._platform_callbacks[domain]([entity])

                    # Apply any pending state that arrived before the entity was created
                    pending_key = (instance_id, remote_eid)
                    if pending_key in self._pending_states:
                        pending = self._pending_states.pop(pending_key)
                        entity.update_state(
                            state=pending["state"],
                            attributes=pending["attributes"],
                        )

                    _LOGGER.debug(
                        "Created shared entity %s from instance %s",
                        entity_data.get("entity_id"),
                        instance_name,
                    )
            else:
                _LOGGER.debug(
                    "Platform %s not yet loaded, entity %s will be created when platform loads",
                    domain,
                    entity_data.get("entity_id"),
                )

    async def _handle_state(self, topic: str, payload: bytes) -> None:
        """Handle incoming state messages."""
        # Parse topic: shared_ha/{instance_id}/states/{entity_id}
        parts = topic.split("/")
        if len(parts) < 4:
            return

        instance_id = parts[1]

        # Skip our own messages
        if instance_id == self._instance_id:
            return

        # Empty payload = entity removed
        if not payload:
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("Invalid state payload on topic %s", topic)
            return

        entity_id = data.get("entity_id", "")
        unique_id_prefix = f"shared_ha_{instance_id}_"

        # Find the matching created entity
        found = False
        for uid, entity in self._created_entities.items():
            if uid.startswith(unique_id_prefix) and hasattr(entity, "_remote_entity_id"):
                if entity._remote_entity_id == entity_id:
                    entity.update_state(
                        state=data.get("state"),
                        attributes=data.get("attributes", {}),
                    )
                    found = True
                    break

        # Cache the state if entity doesn't exist yet (retained messages may arrive before device)
        if not found:
            self._pending_states[(instance_id, entity_id)] = {
                "state": data.get("state"),
                "attributes": data.get("attributes", {}),
            }

    async def _handle_heartbeat(self, topic: str, payload: bytes) -> None:
        """Handle heartbeat messages."""
        parts = topic.split("/")
        if len(parts) < 3:
            return

        instance_id = parts[1]
        if instance_id == self._instance_id:
            return

        if not payload:
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        online = data.get("online", False)
        old_status = self._instance_status.get(instance_id)
        self._instance_status[instance_id] = online

        if old_status is not None and old_status != online:
            _LOGGER.info(
                "Instance %s (%s) is now %s",
                data.get("instance_name", "unknown"),
                instance_id,
                "online" if online else "offline",
            )

            # Update availability of all entities from this instance
            unique_id_prefix = f"shared_ha_{instance_id}_"
            for uid, entity in self._created_entities.items():
                if uid.startswith(unique_id_prefix):
                    entity.set_available(online)

    async def _remove_device(self, instance_id: str, device_id: str) -> None:
        """Remove a remote device and its entities."""
        if instance_id in self._remote_devices:
            self._remote_devices[instance_id].pop(device_id, None)

        # Remove created entities for this device
        to_remove = []
        for uid, entity in self._created_entities.items():
            if (
                hasattr(entity, "_source_instance_id")
                and entity._source_instance_id == instance_id
                and hasattr(entity, "_source_device_id")
                and entity._source_device_id == device_id
            ):
                to_remove.append(uid)
                entity.set_available(False)

        for uid in to_remove:
            self._created_entities.pop(uid, None)

        # Remove device from registry
        dev_reg = dr.async_get(self._hass)
        device = dev_reg.async_get_device(
            identifiers={(DOMAIN, f"{instance_id}_{device_id}")}
        )
        if device:
            dev_reg.async_remove_device(device.id)
            _LOGGER.debug(
                "Removed shared device %s from instance %s", device_id, instance_id
            )

    async def _request_entity_history(
        self, source_instance_id: str, entity_id: str, delay: float = 2.0
    ) -> None:
        """Request history for a shared entity via the history consumer."""
        await asyncio.sleep(max(delay, 2.0))
        try:
            history_consumer = self._config_entry.runtime_data.history_consumer
            _LOGGER.info("Requesting history for %s", entity_id)
            await history_consumer.async_request_history(source_instance_id, entity_id)
        except Exception:
            _LOGGER.warning(
                "Failed to request history for %s from %s",
                entity_id,
                source_instance_id[:8],
                exc_info=True,
            )
