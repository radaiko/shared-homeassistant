"""Number platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared number entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("number", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("number")
    if catch_up:
        async_add_entities(catch_up)


class SharedNumber(SharedBaseEntity, NumberEntity):
    """A shared number entity."""

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the shared number."""
        entity_data = kwargs.get("entity_data", {})
        super().__init__(**kwargs)

        if entity_data.get("unit_of_measurement"):
            self._attr_native_unit_of_measurement = entity_data["unit_of_measurement"]

    @property
    def native_value(self) -> float | None:
        """Return the current value."""
        if self._remote_state is None:
            return None
        try:
            return float(self._remote_state)
        except (ValueError, TypeError):
            return None

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process number state update."""
        if "min" in attributes:
            self._attr_native_min_value = attributes["min"]
        if "max" in attributes:
            self._attr_native_max_value = attributes["max"]
        if "step" in attributes:
            self._attr_native_step = attributes["step"]

    async def async_set_native_value(self, value: float) -> None:
        """Set the value."""
        await self._async_send_command("number.set_value", {"value": value})
