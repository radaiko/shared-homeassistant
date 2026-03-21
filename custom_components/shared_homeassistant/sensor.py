"""Sensor platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity
from .const import DOMAIN, DATA_SUBSCRIBER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared sensor entities."""
    subscriber = hass.data[DOMAIN][config_entry.entry_id][DATA_SUBSCRIBER]
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

    @property
    def native_value(self) -> str | None:
        """Return the sensor value."""
        return self._remote_state

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process sensor state update."""
        if "unit_of_measurement" in attributes:
            self._attr_native_unit_of_measurement = attributes.get(
                "unit_of_measurement"
            )
