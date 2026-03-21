"""Publisher for Shared Home Assistant - publishes local devices/entities to MQTT."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.core import HomeAssistant, Event, callback, State
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .const import (
    TOPIC_PREFIX,
    TOPIC_DEVICE,
    TOPIC_STATE,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_NAME,
    CONF_SELECTED_DEVICES,
    CONF_SELECTED_ENTITIES,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)


class Publisher:
    """Publishes selected devices and entities to the shared MQTT broker."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: MQTTClient,
        config: dict[str, Any],
    ) -> None:
        """Initialize the publisher."""
        self._hass = hass
        self._mqtt = mqtt_client
        self._instance_id: str = config[CONF_INSTANCE_ID]
        self._instance_name: str = config[CONF_INSTANCE_NAME]
        self._selected_devices: list[str] = config.get(CONF_SELECTED_DEVICES, [])
        self._selected_entities: list[str] = config.get(CONF_SELECTED_ENTITIES, [])
        self._unsub_state_listener: callback | None = None
        self._published_devices: set[str] = set()
        self._published_entities: set[str] = set()

    async def async_start(self) -> None:
        """Start publishing selected devices and entities."""
        # Subscribe to command topic for this instance
        command_topic = f"{TOPIC_PREFIX}/{self._instance_id}/commands/#"
        await self._mqtt.async_subscribe(command_topic, self._handle_command)

        # Publish all selected devices and their entities
        await self._publish_all_devices()

        # Publish all individually selected entities
        await self._publish_selected_entities()

        # Listen for state changes
        self._unsub_state_listener = self._hass.bus.async_listen(
            "state_changed", self._handle_state_changed
        )

    async def async_stop(self) -> None:
        """Stop publishing and clear retained messages."""
        if self._unsub_state_listener:
            self._unsub_state_listener()
            self._unsub_state_listener = None

        # Clear all published retained messages
        for device_id in self._published_devices:
            topic = TOPIC_DEVICE.format(
                instance_id=self._instance_id, device_id=device_id
            )
            await self._mqtt.async_publish(topic, "", retain=True)

        for entity_id in self._published_entities:
            topic = TOPIC_STATE.format(
                instance_id=self._instance_id, entity_id=entity_id
            )
            await self._mqtt.async_publish(topic, "", retain=True)

    async def async_update_selection(
        self,
        selected_devices: list[str],
        selected_entities: list[str],
    ) -> None:
        """Update the selection and publish/unpublish as needed."""
        old_devices = set(self._selected_devices)
        new_devices = set(selected_devices)
        old_entities = set(self._selected_entities)
        new_entities = set(selected_entities)

        # Unpublish removed devices
        dev_reg = dr.async_get(self._hass)
        for device_id in old_devices - new_devices:
            device = dev_reg.async_get(device_id)
            if device:
                await self._unpublish_device(device)

        # Unpublish removed entities
        for entity_id in old_entities - new_entities:
            await self._unpublish_entity(entity_id)

        self._selected_devices = selected_devices
        self._selected_entities = selected_entities

        # Publish new devices
        for device_id in new_devices - old_devices:
            device = dev_reg.async_get(device_id)
            if device:
                await self._publish_device(device)

        # Publish new entities
        ent_reg = er.async_get(self._hass)
        for entity_id in new_entities - old_entities:
            entry = ent_reg.async_get(entity_id)
            if entry:
                await self._publish_entity_state(entity_id)

    async def _publish_all_devices(self) -> None:
        """Publish all selected devices with their entities."""
        dev_reg = dr.async_get(self._hass)

        for device_id in self._selected_devices:
            device = dev_reg.async_get(device_id)
            if device is None:
                _LOGGER.warning("Selected device %s not found in registry", device_id)
                continue
            await self._publish_device(device)

    async def _publish_device(self, device: dr.DeviceEntry) -> None:
        """Publish a single device and all its entities."""
        ent_reg = er.async_get(self._hass)
        entities = er.async_entries_for_device(ent_reg, device.id)

        entity_list = []
        for entry in entities:
            state = self._hass.states.get(entry.entity_id)
            entity_data = {
                "entity_id": entry.entity_id,
                "unique_id": entry.unique_id,
                "domain": entry.domain,
                "name": entry.name or entry.original_name or "",
                "device_class": entry.device_class or entry.original_device_class,
                "unit_of_measurement": entry.unit_of_measurement,
                "icon": entry.icon or entry.original_icon,
                "attributes": dict(state.attributes) if state else {},
            }
            entity_list.append(entity_data)

            # Also publish initial state
            if state:
                await self._publish_entity_state(entry.entity_id, state)

        # Build device identifiers as serializable lists
        identifiers = [list(i) for i in device.identifiers] if device.identifiers else []
        connections = [list(c) for c in device.connections] if device.connections else []

        payload = {
            "instance_id": self._instance_id,
            "instance_name": self._instance_name,
            "device_id": device.id,
            "name": device.name or "",
            "manufacturer": device.manufacturer,
            "model": device.model,
            "sw_version": device.sw_version,
            "hw_version": device.hw_version,
            "identifiers": identifiers,
            "connections": connections,
            "entities": entity_list,
        }

        topic = TOPIC_DEVICE.format(
            instance_id=self._instance_id, device_id=device.id
        )
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=True)
        self._published_devices.add(device.id)
        _LOGGER.debug("Published device %s (%s)", device.name, device.id)

    async def _publish_selected_entities(self) -> None:
        """Publish individually selected entities (not part of a device)."""
        for entity_id in self._selected_entities:
            state = self._hass.states.get(entity_id)
            if state:
                await self._publish_entity_state(entity_id, state)

    async def _publish_entity_state(
        self, entity_id: str, state: State | None = None
    ) -> None:
        """Publish entity state to MQTT."""
        if state is None:
            state = self._hass.states.get(entity_id)
        if state is None:
            return

        payload = {
            "instance_id": self._instance_id,
            "entity_id": entity_id,
            "state": state.state,
            "attributes": _serialize_attributes(state.attributes),
            "last_changed": state.last_changed.isoformat(),
        }

        topic = TOPIC_STATE.format(
            instance_id=self._instance_id, entity_id=entity_id
        )
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=True)
        self._published_entities.add(entity_id)

    async def _unpublish_device(self, device: dr.DeviceEntry) -> None:
        """Unpublish a device by sending empty retained message."""
        topic = TOPIC_DEVICE.format(
            instance_id=self._instance_id, device_id=device.id
        )
        await self._mqtt.async_publish(topic, "", retain=True)
        self._published_devices.discard(device.id)

        # Also unpublish all entity states for this device
        ent_reg = er.async_get(self._hass)
        entities = er.async_entries_for_device(ent_reg, device.id)
        for entry in entities:
            await self._unpublish_entity(entry.entity_id)

    async def _unpublish_entity(self, entity_id: str) -> None:
        """Unpublish an entity by sending empty retained message."""
        topic = TOPIC_STATE.format(
            instance_id=self._instance_id, entity_id=entity_id
        )
        await self._mqtt.async_publish(topic, "", retain=True)
        self._published_entities.discard(entity_id)

    async def _handle_state_changed(self, event: Event) -> None:
        """Handle state_changed events for published entities."""
        entity_id = event.data.get("entity_id")
        if entity_id is None:
            return

        # Check if this entity is published (either via device or directly selected)
        if entity_id not in self._published_entities:
            return

        new_state = event.data.get("new_state")
        if new_state is None:
            return

        await self._publish_entity_state(entity_id, new_state)

    async def _handle_command(self, topic: str, payload: bytes) -> None:
        """Handle incoming command messages from other instances."""
        if not payload:
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("Invalid command payload on topic %s", topic)
            return

        service = data.get("service", "")
        service_data = data.get("service_data", {})

        if "." not in service:
            _LOGGER.warning("Invalid service format in command: %s", service)
            return

        domain, service_name = service.split(".", 1)

        try:
            await self._hass.services.async_call(
                domain, service_name, service_data, blocking=True
            )
            _LOGGER.debug("Executed command %s with data %s", service, service_data)
        except Exception:
            _LOGGER.exception("Failed to execute command %s", service)


def _serialize_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Serialize state attributes to JSON-safe dict."""
    result = {}
    for key, value in attributes.items():
        try:
            json.dumps(value)
            result[key] = value
        except (TypeError, ValueError):
            result[key] = str(value)
    return result
