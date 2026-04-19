"""Config flow for Shared Home Assistant v2."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .const import (
    CONF_INSTANCE_ID,
    CONF_INSTANCE_NAME,
    CONF_OWN_BROKER_HOST,
    CONF_OWN_BROKER_PASSWORD,
    CONF_OWN_BROKER_PORT,
    CONF_OWN_BROKER_TLS,
    CONF_OWN_BROKER_USERNAME,
    CONF_PEER_BROKER_HOST,
    CONF_PEER_BROKER_PASSWORD,
    CONF_PEER_BROKER_PORT,
    CONF_PEER_BROKER_TLS,
    CONF_PEER_BROKER_USERNAME,
    CONF_PEER_DISCOVERY_PREFIX,
    CONF_READONLY_DEVICES,
    CONF_READONLY_ENTITIES,
    CONF_READONLY_INTEGRATIONS,
    CONF_SHARED_DEVICES,
    CONF_SHARED_ENTITIES,
    CONF_SHARED_INTEGRATIONS,
    DEFAULT_BROKER_PORT,
    DEFAULT_DISCOVERY_PREFIX,
    DOMAIN,
    SUPPORTED_DOMAINS,
)

_LOGGER = logging.getLogger(__name__)


def _broker_schema(prefix: str, defaults: dict[str, Any]) -> dict:
    """Build a broker-subform schema using the given key prefix."""
    return {
        vol.Required(
            f"{prefix}_host", default=defaults.get(f"{prefix}_host", "")
        ): TextSelector(TextSelectorConfig(type="text")),
        vol.Required(
            f"{prefix}_port",
            default=defaults.get(f"{prefix}_port", DEFAULT_BROKER_PORT),
        ): NumberSelector(
            NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
        ),
        vol.Optional(
            f"{prefix}_username",
            default=defaults.get(f"{prefix}_username", ""),
        ): TextSelector(TextSelectorConfig(type="text")),
        vol.Optional(
            f"{prefix}_password",
            default=defaults.get(f"{prefix}_password", ""),
        ): TextSelector(TextSelectorConfig(type="password")),
        vol.Required(
            f"{prefix}_tls", default=defaults.get(f"{prefix}_tls", False)
        ): BooleanSelector(),
    }


def _integration_options(hass) -> list[SelectOptionDict]:
    """Build selector options for shareable integrations (config entries)."""
    options: list[SelectOptionDict] = []
    for entry in hass.config_entries.async_entries():
        if entry.domain in {"mqtt", DOMAIN}:
            continue
        label = entry.title or entry.domain
        if entry.title and entry.title != entry.domain:
            label = f"{entry.title} ({entry.domain})"
        options.append(SelectOptionDict(value=entry.entry_id, label=label))
    options.sort(key=lambda o: o["label"].lower())
    return options


class SharedHAConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial config flow for Shared Home Assistant v2."""

    VERSION = 2

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: own broker + instance identity."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_peer()

        instance_id = str(uuid.uuid4())

        schema = vol.Schema(
            {
                vol.Required(CONF_INSTANCE_NAME): TextSelector(
                    TextSelectorConfig(type="text")
                ),
                vol.Required(
                    CONF_INSTANCE_ID, default=instance_id
                ): TextSelector(TextSelectorConfig(type="text")),
                **_broker_schema("own_broker", {}),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_peer(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: peer broker."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_selection()

        schema = vol.Schema(
            {
                **_broker_schema("peer_broker", {}),
                vol.Required(
                    CONF_PEER_DISCOVERY_PREFIX, default=DEFAULT_DISCOVERY_PREFIX
                ): TextSelector(TextSelectorConfig(type="text")),
            }
        )
        return self.async_show_form(step_id="peer", data_schema=schema)

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: share selection + readonly flags."""
        if user_input is not None:
            self._data.update(user_input)
            self._data[CONF_OWN_BROKER_PORT] = int(self._data[CONF_OWN_BROKER_PORT])
            self._data[CONF_PEER_BROKER_PORT] = int(self._data[CONF_PEER_BROKER_PORT])
            return self.async_create_entry(
                title=self._data[CONF_INSTANCE_NAME],
                data=self._data,
            )

        return self.async_show_form(
            step_id="selection",
            data_schema=_selection_schema(self.hass, {}),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return SharedHAOptionsFlow()


def _selection_schema(hass, defaults: dict[str, Any]) -> vol.Schema:
    """Schema for share-selection step."""
    integration_options = _integration_options(hass)
    domains = sorted(SUPPORTED_DOMAINS)

    return vol.Schema(
        {
            vol.Optional(
                CONF_SHARED_INTEGRATIONS,
                default=defaults.get(CONF_SHARED_INTEGRATIONS, []),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=integration_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_READONLY_INTEGRATIONS,
                default=defaults.get(CONF_READONLY_INTEGRATIONS, []),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=integration_options,
                    multiple=True,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_SHARED_DEVICES,
                default=defaults.get(CONF_SHARED_DEVICES, []),
            ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_READONLY_DEVICES,
                default=defaults.get(CONF_READONLY_DEVICES, []),
            ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_SHARED_ENTITIES,
                default=defaults.get(CONF_SHARED_ENTITIES, []),
            ): EntitySelector(
                EntitySelectorConfig(multiple=True, domain=domains)
            ),
            vol.Optional(
                CONF_READONLY_ENTITIES,
                default=defaults.get(CONF_READONLY_ENTITIES, []),
            ): EntitySelector(
                EntitySelectorConfig(multiple=True, domain=domains)
            ),
        }
    )


class SharedHAOptionsFlow(OptionsFlow):
    """Options flow — tweak share selection and broker details after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit share selection."""
        if user_input is not None:
            existing = dict(self.config_entry.data)
            merged = {**existing, **user_input}
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=merged
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=_selection_schema(self.hass, dict(self.config_entry.data)),
        )
