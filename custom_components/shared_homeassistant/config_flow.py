"""Config flow for Shared Home Assistant integration."""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    TextSelector,
    TextSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    BooleanSelector,
)

from .const import (
    DOMAIN,
    CONF_BROKER_HOST,
    CONF_BROKER_PORT,
    CONF_BROKER_USERNAME,
    CONF_BROKER_PASSWORD,
    CONF_USE_TLS,
    CONF_INSTANCE_NAME,
    CONF_INSTANCE_ID,
    CONF_SELECTED_DEVICES,
    CONF_SELECTED_ENTITIES,
    CONF_ENTITY_PREFIX,
    DEFAULT_PORT,
    DEFAULT_ENTITY_PREFIX,
    PLATFORMS,
)


class SharedHAConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shared Home Assistant."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._data: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: MQTT broker configuration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_instance()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BROKER_HOST): TextSelector(
                        TextSelectorConfig(type="text")
                    ),
                    vol.Required(
                        CONF_BROKER_PORT, default=DEFAULT_PORT
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=65535, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Optional(CONF_BROKER_USERNAME, default=""): TextSelector(
                        TextSelectorConfig(type="text")
                    ),
                    vol.Optional(CONF_BROKER_PASSWORD, default=""): TextSelector(
                        TextSelectorConfig(type="password")
                    ),
                    vol.Required(CONF_USE_TLS, default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def async_step_instance(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Instance identification."""
        if user_input is not None:
            self._data[CONF_INSTANCE_NAME] = user_input[CONF_INSTANCE_NAME]
            self._data[CONF_INSTANCE_ID] = user_input.get(
                CONF_INSTANCE_ID, str(uuid.uuid4())
            )
            return await self.async_step_selection()

        instance_id = str(uuid.uuid4())

        return self.async_show_form(
            step_id="instance",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_INSTANCE_NAME): TextSelector(
                        TextSelectorConfig(type="text")
                    ),
                    vol.Required(
                        CONF_INSTANCE_ID, default=instance_id
                    ): TextSelector(TextSelectorConfig(type="text")),
                }
            ),
            description_placeholders={"instance_id": instance_id},
        )

    async def async_step_selection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 3: Device and entity selection."""
        if user_input is not None:
            self._data[CONF_SELECTED_DEVICES] = user_input.get(
                CONF_SELECTED_DEVICES, []
            )
            self._data[CONF_SELECTED_ENTITIES] = user_input.get(
                CONF_SELECTED_ENTITIES, []
            )

            # Ensure port is int
            self._data[CONF_BROKER_PORT] = int(self._data[CONF_BROKER_PORT])

            return self.async_create_entry(
                title=self._data[CONF_INSTANCE_NAME],
                data=self._data,
            )

        return self.async_show_form(
            step_id="selection",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_DEVICES, default=[]
                    ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_SELECTED_ENTITIES, default=[]
                    ): EntitySelector(
                        EntitySelectorConfig(
                            multiple=True,
                            domain=PLATFORMS,
                        )
                    ),
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SharedHAOptionsFlow()


class SharedHAOptionsFlow(OptionsFlow):
    """Handle options flow for Shared Home Assistant."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            new_data = {**self.config_entry.data, **user_input}
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_SELECTED_DEVICES,
                        default=current.get(CONF_SELECTED_DEVICES, []),
                    ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
                    vol.Optional(
                        CONF_SELECTED_ENTITIES,
                        default=current.get(CONF_SELECTED_ENTITIES, []),
                    ): EntitySelector(
                        EntitySelectorConfig(
                            multiple=True,
                            domain=PLATFORMS,
                        )
                    ),
                    vol.Optional(
                        CONF_ENTITY_PREFIX,
                        default=current.get(CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX),
                    ): TextSelector(TextSelectorConfig(type="text")),
                }
            ),
        )
