"""Config flow for Shared Home Assistant integration."""

from __future__ import annotations

import logging
import uuid
from typing import Any

_LOGGER = logging.getLogger(__name__)

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, OptionsFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    SelectOptionDict,
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
    CONF_READONLY_DEVICES,
    CONF_READONLY_ENTITIES,
    CONF_ENTITY_PREFIX,
    CONF_SHARE_DASHBOARDS,
    CONF_SHARED_DASHBOARD_LIST,
    CONF_INSTANCE_URL,
    DEFAULT_PORT,
    DEFAULT_ENTITY_PREFIX,
    PLATFORMS,
)


def _selection_schema(
    devices_rw: list = [],
    devices_ro: list = [],
    entities_rw: list = [],
    entities_ro: list = [],
) -> vol.Schema:
    """Build the device/entity selection schema."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_SELECTED_DEVICES, default=devices_rw
            ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_READONLY_DEVICES, default=devices_ro
            ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
            vol.Optional(
                CONF_SELECTED_ENTITIES, default=entities_rw
            ): EntitySelector(
                EntitySelectorConfig(multiple=True, domain=PLATFORMS)
            ),
            vol.Optional(
                CONF_READONLY_ENTITIES, default=entities_ro
            ): EntitySelector(
                EntitySelectorConfig(multiple=True, domain=PLATFORMS)
            ),
        }
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
            self._data[CONF_READONLY_DEVICES] = user_input.get(
                CONF_READONLY_DEVICES, []
            )
            self._data[CONF_SELECTED_ENTITIES] = user_input.get(
                CONF_SELECTED_ENTITIES, []
            )
            self._data[CONF_READONLY_ENTITIES] = user_input.get(
                CONF_READONLY_ENTITIES, []
            )

            # Ensure port is int
            self._data[CONF_BROKER_PORT] = int(self._data[CONF_BROKER_PORT])

            return self.async_create_entry(
                title=self._data[CONF_INSTANCE_NAME],
                data=self._data,
            )

        return self.async_show_form(
            step_id="selection",
            data_schema=_selection_schema(),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return SharedHAOptionsFlow()


class SharedHAOptionsFlow(OptionsFlow):
    """Handle options flow for Shared Home Assistant."""

    def __init__(self) -> None:
        """Initialize options flow."""
        self._data: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 1: Entity sharing options."""
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_dashboards()

        current = self.config_entry.data

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SELECTED_DEVICES,
                    default=current.get(CONF_SELECTED_DEVICES, []),
                ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
                vol.Optional(
                    CONF_READONLY_DEVICES,
                    default=current.get(CONF_READONLY_DEVICES, []),
                ): DeviceSelector(DeviceSelectorConfig(multiple=True)),
                vol.Optional(
                    CONF_SELECTED_ENTITIES,
                    default=current.get(CONF_SELECTED_ENTITIES, []),
                ): EntitySelector(
                    EntitySelectorConfig(multiple=True, domain=PLATFORMS)
                ),
                vol.Optional(
                    CONF_READONLY_ENTITIES,
                    default=current.get(CONF_READONLY_ENTITIES, []),
                ): EntitySelector(
                    EntitySelectorConfig(multiple=True, domain=PLATFORMS)
                ),
                vol.Optional(
                    CONF_ENTITY_PREFIX,
                    default=current.get(CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX),
                ): TextSelector(TextSelectorConfig(type="text")),
            }
        )

        return self.async_show_form(step_id="init", data_schema=schema)

    async def async_step_dashboards(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step 2: Dashboard sharing options."""
        if user_input is not None:
            self._data.update(user_input)
            # Merge with existing config entry data (preserve broker/instance config)
            existing = dict(self.config_entry.data) if self.config_entry.data else {}
            if not existing:
                _LOGGER.error("Config entry data is empty! Cannot save options.")
                return self.async_create_entry(title="", data={})
            new_data = {**existing, **self._data}
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            return self.async_create_entry(title="", data={})

        current = self.config_entry.data

        # Build dashboard options dynamically
        dashboard_options = await self._get_dashboard_options()

        schema_dict: dict[Any, Any] = {
            vol.Optional(
                CONF_SHARE_DASHBOARDS,
                default=current.get(CONF_SHARE_DASHBOARDS, False),
            ): BooleanSelector(),
            vol.Optional(
                CONF_INSTANCE_URL,
                default=current.get(CONF_INSTANCE_URL, ""),
            ): TextSelector(TextSelectorConfig(type="url")),
        }

        if dashboard_options:
            schema_dict[vol.Optional(
                CONF_SHARED_DASHBOARD_LIST,
                default=current.get(CONF_SHARED_DASHBOARD_LIST, []),
            )] = SelectSelector(
                SelectSelectorConfig(
                    options=dashboard_options,
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )

        return self.async_show_form(
            step_id="dashboards",
            data_schema=vol.Schema(schema_dict),
        )

    async def _get_dashboard_options(self) -> list[SelectOptionDict]:
        """Get available dashboards as selector options."""
        options: list[SelectOptionDict] = []
        try:
            from homeassistant.components.lovelace.const import LOVELACE_DATA

            lovelace_data = self.hass.data.get(LOVELACE_DATA)
            if lovelace_data is None:
                return options

            for url_path, dashboard in lovelace_data.dashboards.items():
                if url_path is None:
                    options.append(
                        SelectOptionDict(value="lovelace", label="Overview (default)")
                    )
                else:
                    title = url_path
                    if hasattr(dashboard, "config") and isinstance(
                        dashboard.config, dict
                    ):
                        title = dashboard.config.get("title", url_path)
                    options.append(
                        SelectOptionDict(value=url_path, label=title)
                    )
        except Exception:
            pass
        return options
