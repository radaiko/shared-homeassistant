"""Sensor platform for Shared Home Assistant."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared sensor entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("sensor", async_add_entities)

    # Add any entities that arrived before platform was loaded
    catch_up = subscriber.get_entities_for_domain("sensor")
    if catch_up:
        async_add_entities(catch_up)


class SharedSensor(SharedBaseEntity, SensorEntity):
    """A shared sensor entity."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the shared sensor."""
        entity_data = kwargs.get("entity_data", {})
        super().__init__(**kwargs)

        if entity_data.get("unit_of_measurement"):
            self._attr_native_unit_of_measurement = entity_data["unit_of_measurement"]

        # Copy state_class from source to preserve statistics behavior
        attrs = entity_data.get("attributes", {})
        if attrs.get("state_class"):
            self._attr_state_class = attrs["state_class"]

        # Set high display precision to avoid rounding
        self._attr_suggested_display_precision = 1

    @property
    def native_value(self) -> str | float | datetime | None:
        """Return the sensor value."""
        if self._remote_state is None or self._remote_state in (
            "unavailable", "unknown",
        ):
            return None

        # Timestamp sensors require a datetime object
        device_class = getattr(self, "_attr_device_class", None)
        if device_class == SensorDeviceClass.TIMESTAMP:
            try:
                return datetime.fromisoformat(self._remote_state)
            except (ValueError, TypeError):
                return None

        return self._remote_state

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process sensor state update."""
        if "unit_of_measurement" in attributes:
            self._attr_native_unit_of_measurement = attributes.get(
                "unit_of_measurement"
            )
        if "state_class" in attributes:
            self._attr_state_class = attributes["state_class"]
