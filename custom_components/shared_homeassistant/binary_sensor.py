"""Binary sensor platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared binary sensor entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("binary_sensor", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("binary_sensor")
    if catch_up:
        async_add_entities(catch_up)


class SharedBinarySensor(SharedBaseEntity, BinarySensorEntity):
    """A shared binary sensor entity."""

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if self._remote_state is None:
            return None
        return self._remote_state == "on"
