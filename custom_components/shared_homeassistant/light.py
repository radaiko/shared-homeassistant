"""Light platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    LightEntity,
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ATTR_HS_COLOR,
    ColorMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared light entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("light", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("light")
    if catch_up:
        async_add_entities(catch_up)


class SharedLight(SharedBaseEntity, LightEntity):
    """A shared light entity."""

    _attr_color_mode = ColorMode.UNKNOWN
    _attr_supported_color_modes = {ColorMode.ONOFF}

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the shared light."""
        super().__init__(**kwargs)
        self._brightness: int | None = None
        self._color_temp_kelvin: int | None = None
        self._rgb_color: tuple[int, int, int] | None = None
        self._hs_color: tuple[float, float] | None = None

    @property
    def is_on(self) -> bool | None:
        """Return true if the light is on."""
        if self._remote_state is None:
            return None
        return self._remote_state == "on"

    @property
    def brightness(self) -> int | None:
        """Return the brightness."""
        return self._brightness

    @property
    def color_temp_kelvin(self) -> int | None:
        """Return the color temperature in Kelvin."""
        return self._color_temp_kelvin

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the RGB color."""
        return self._rgb_color

    @property
    def hs_color(self) -> tuple[float, float] | None:
        """Return the HS color."""
        return self._hs_color

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process light state update."""
        self._brightness = attributes.get(ATTR_BRIGHTNESS)
        self._color_temp_kelvin = attributes.get(ATTR_COLOR_TEMP_KELVIN)
        self._rgb_color = attributes.get(ATTR_RGB_COLOR)
        self._hs_color = attributes.get(ATTR_HS_COLOR)

        # Update supported color modes from attributes
        supported_modes = attributes.get("supported_color_modes")
        if supported_modes:
            valid = set()
            for m in supported_modes:
                try:
                    valid.add(ColorMode(m))
                except ValueError:
                    pass
            if valid:
                self._attr_supported_color_modes = valid
        color_mode = attributes.get("color_mode")
        if color_mode:
            try:
                self._attr_color_mode = ColorMode(color_mode)
            except ValueError:
                pass

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        service_data = {}
        if ATTR_BRIGHTNESS in kwargs:
            service_data[ATTR_BRIGHTNESS] = kwargs[ATTR_BRIGHTNESS]
        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            service_data[ATTR_COLOR_TEMP_KELVIN] = kwargs[ATTR_COLOR_TEMP_KELVIN]
        if ATTR_RGB_COLOR in kwargs:
            service_data[ATTR_RGB_COLOR] = list(kwargs[ATTR_RGB_COLOR])
        if ATTR_HS_COLOR in kwargs:
            service_data[ATTR_HS_COLOR] = list(kwargs[ATTR_HS_COLOR])
        await self._async_send_command("light.turn_on", service_data or None)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        await self._async_send_command("light.turn_off")

    async def async_toggle(self, **kwargs: Any) -> None:
        """Toggle the light."""
        await self._async_send_command("light.toggle")
