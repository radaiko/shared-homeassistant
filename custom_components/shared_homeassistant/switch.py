"""Switch platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared switch entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("switch", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("switch")
    if catch_up:
        async_add_entities(catch_up)


class SharedSwitch(SharedBaseEntity, SwitchEntity):
    """A shared switch entity."""

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self._remote_state is None:
            return None
        return self._remote_state == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_send_command("switch.turn_on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_send_command("switch.turn_off")

    async def async_toggle(self, **kwargs: Any) -> None:
        """Toggle the switch."""
        await self._async_send_command("switch.toggle")
