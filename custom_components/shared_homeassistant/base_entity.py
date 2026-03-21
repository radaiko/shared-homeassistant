"""Base entity for Shared Home Assistant shared entities."""

from __future__ import annotations

import json
import logging
from typing import Any

from homeassistant.helpers.entity import Entity

from .const import DOMAIN, TOPIC_COMMAND
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)


class SharedBaseEntity(Entity):
    """Base class for all shared entities."""

    _attr_should_poll = False
    _attr_has_entity_name = False

    def __init__(
        self,
        entity_data: dict[str, Any],
        instance_id: str,
        instance_name: str,
        device_id: str,
        mqtt_client: MQTTClient,
        entity_prefix: str,
    ) -> None:
        """Initialize the shared entity."""
        self._mqtt = mqtt_client
        self._source_instance_id = instance_id
        self._source_instance_name = instance_name
        self._source_device_id = device_id
        self._remote_entity_id = entity_data["entity_id"]
        self._entity_prefix = entity_prefix
        self._readonly = entity_data.get("readonly", False)

        # Build unique_id
        remote_unique = entity_data.get("unique_id", entity_data["entity_id"])
        self._attr_unique_id = f"shared_ha_{instance_id}_{remote_unique}"

        # Use the source entity_id directly so the local entity gets the same ID
        remote_entity_id = entity_data["entity_id"]
        if entity_prefix:
            # With a prefix: sensor.living_room → sensor.{prefix}_living_room
            domain_part, object_id = remote_entity_id.split(".", 1)
            self.entity_id = f"{domain_part}.{entity_prefix}_{object_id}"
        else:
            self.entity_id = remote_entity_id

        # Use the source entity's friendly name from attributes, or fall back to the name field
        friendly_name = entity_data.get("attributes", {}).get("friendly_name")
        name = friendly_name or entity_data.get("name") or remote_entity_id.split(".", 1)[-1]
        if entity_prefix:
            self._attr_name = f"{entity_prefix} {name}"
        else:
            self._attr_name = name

        # Device class and icon
        if entity_data.get("device_class"):
            self._attr_device_class = entity_data["device_class"]
        if entity_data.get("icon"):
            self._attr_icon = entity_data["icon"]

        # Initial state
        self._remote_state: str | None = None
        self._remote_attributes: dict[str, Any] = entity_data.get("attributes", {})
        self._attr_available = True

    @property
    def device_info(self):
        """Return device info to link entity to device."""
        return {
            "identifiers": {
                (DOMAIN, f"{self._source_instance_id}_{self._source_device_id}")
            },
        }

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = dict(self._remote_attributes)
        attrs["shared_from_instance"] = self._source_instance_name
        attrs["shared_from_entity"] = self._remote_entity_id
        return attrs

    def update_from_payload(self, entity_data: dict[str, Any]) -> None:
        """Update entity metadata from a device payload."""
        if entity_data.get("device_class"):
            self._attr_device_class = entity_data["device_class"]
        if entity_data.get("icon"):
            self._attr_icon = entity_data["icon"]
        if self.hass:
            self.async_write_ha_state()

    def update_state(self, state: str | None, attributes: dict[str, Any]) -> None:
        """Update entity state from a state message."""
        self._remote_state = state
        self._remote_attributes = attributes
        self._process_state_update(state, attributes)
        if self.hass:
            self.async_write_ha_state()

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process state update. Override in subclasses for domain-specific logic."""

    def set_available(self, available: bool) -> None:
        """Set entity availability."""
        self._attr_available = available
        if self.hass:
            self.async_write_ha_state()

    async def _async_send_command(
        self, service: str, service_data: dict[str, Any] | None = None
    ) -> None:
        """Send a command to the origin instance via MQTT."""
        if self._readonly:
            _LOGGER.warning(
                "Cannot send command %s to read-only entity %s",
                service,
                self._remote_entity_id,
            )
            return
        data = service_data or {}
        data["entity_id"] = self._remote_entity_id

        payload = {
            "service": service,
            "service_data": data,
        }

        topic = TOPIC_COMMAND.format(
            instance_id=self._source_instance_id,
            entity_id=self._remote_entity_id,
        )
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=False)
