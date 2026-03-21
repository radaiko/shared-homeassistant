"""Shared Home Assistant - Share devices and entities between HA instances via MQTT."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_BROKER_HOST,
    CONF_BROKER_PORT,
    CONF_BROKER_USERNAME,
    CONF_BROKER_PASSWORD,
    CONF_USE_TLS,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_NAME,
    CONF_SELECTED_DEVICES,
    CONF_SELECTED_ENTITIES,
    PLATFORMS,
    SharedHARuntimeData,
)
from .mqtt_client import MQTTClient
from .publisher import Publisher
from .subscriber import Subscriber

type SharedHAConfigEntry = ConfigEntry[SharedHARuntimeData]

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: SharedHAConfigEntry) -> bool:
    """Set up Shared Home Assistant from a config entry."""
    data = entry.data

    # Create MQTT client
    mqtt_client = MQTTClient(
        host=data[CONF_BROKER_HOST],
        port=int(data[CONF_BROKER_PORT]),
        instance_id=data[CONF_INSTANCE_ID],
        instance_name=data[CONF_INSTANCE_NAME],
        username=data.get(CONF_BROKER_USERNAME) or None,
        password=data.get(CONF_BROKER_PASSWORD) or None,
        use_tls=data.get(CONF_USE_TLS, False),
    )

    # Create publisher and subscriber
    publisher = Publisher(hass, mqtt_client, data)
    subscriber = Subscriber(hass, mqtt_client, entry)

    # Store in runtime_data
    entry.runtime_data = SharedHARuntimeData(
        mqtt_client=mqtt_client,
        publisher=publisher,
        subscriber=subscriber,
    )

    # Connect to MQTT
    try:
        await mqtt_client.async_connect()
    except Exception:
        _LOGGER.exception("Failed to connect to MQTT broker %s", data[CONF_BROKER_HOST])
        _LOGGER.warning("Will retry MQTT connection in background")

    # Set up platforms (this triggers async_setup_entry in each platform file)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start publisher and subscriber after platforms are loaded
    if mqtt_client.connected:
        await publisher.async_start()
        await subscriber.async_start()
    else:
        async def _start_when_connected():
            """Start publisher/subscriber once MQTT connects."""
            try:
                await asyncio.wait_for(mqtt_client._connected.wait(), timeout=60)
                await publisher.async_start()
                await subscriber.async_start()
            except asyncio.TimeoutError:
                _LOGGER.warning("Timed out waiting for MQTT connection, will retry")

        hass.async_create_task(_start_when_connected())

    # Register options update listener
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: SharedHAConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        rt = entry.runtime_data
        await rt.publisher.async_stop()
        await rt.subscriber.async_stop()
        await rt.mqtt_client.async_disconnect()

    return unload_ok


async def _async_update_listener(
    hass: HomeAssistant, entry: SharedHAConfigEntry
) -> None:
    """Handle options update."""
    rt = entry.runtime_data
    await rt.publisher.async_update_selection(
        selected_devices=entry.data.get(CONF_SELECTED_DEVICES, []),
        selected_entities=entry.data.get(CONF_SELECTED_ENTITIES, []),
    )
