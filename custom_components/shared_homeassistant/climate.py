"""Climate platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
    ATTR_HVAC_MODE,
    ATTR_TEMPERATURE,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .base_entity import SharedBaseEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up shared climate entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("climate", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("climate")
    if catch_up:
        async_add_entities(catch_up)


class SharedClimate(SharedBaseEntity, ClimateEntity):
    """A shared climate entity."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.FAN_MODE
    )
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.AUTO,
        HVACMode.HEAT_COOL,
    ]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the shared climate."""
        super().__init__(**kwargs)
        self._hvac_mode: HVACMode = HVACMode.OFF
        self._target_temperature: float | None = None
        self._current_temperature: float | None = None
        self._fan_mode: str | None = None
        self._fan_modes: list[str] | None = None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode."""
        return self._hvac_mode

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        return self._target_temperature

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._current_temperature

    @property
    def fan_mode(self) -> str | None:
        """Return the fan mode."""
        return self._fan_mode

    @property
    def fan_modes(self) -> list[str] | None:
        """Return the list of available fan modes."""
        return self._fan_modes

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process climate state update."""
        if state:
            try:
                self._hvac_mode = HVACMode(state)
            except ValueError:
                self._hvac_mode = HVACMode.OFF

        self._target_temperature = attributes.get("temperature")
        self._current_temperature = attributes.get("current_temperature")
        self._fan_mode = attributes.get("fan_mode")
        self._fan_modes = attributes.get("fan_modes")

        hvac_modes = attributes.get("hvac_modes")
        if hvac_modes:
            valid = []
            for m in hvac_modes:
                try:
                    valid.append(HVACMode(m))
                except ValueError:
                    pass
            if valid:
                self._attr_hvac_modes = valid

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        await self._async_send_command(
            "climate.set_hvac_mode", {"hvac_mode": hvac_mode.value}
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        service_data = {}
        if ATTR_TEMPERATURE in kwargs:
            service_data["temperature"] = kwargs[ATTR_TEMPERATURE]
        if ATTR_HVAC_MODE in kwargs:
            service_data["hvac_mode"] = kwargs[ATTR_HVAC_MODE]
        await self._async_send_command("climate.set_temperature", service_data)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Set the fan mode."""
        await self._async_send_command(
            "climate.set_fan_mode", {"fan_mode": fan_mode}
        )
