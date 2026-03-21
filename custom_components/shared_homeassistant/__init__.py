"""Shared Home Assistant - Share devices and entities between HA instances via MQTT."""

from __future__ import annotations

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
    DATA_MQTT_CLIENT,
    DATA_PUBLISHER,
    DATA_SUBSCRIBER,
    PLATFORMS,
)
from .mqtt_client import MQTTClient
from .publisher import Publisher
from .subscriber import Subscriber

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Shared Home Assistant component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
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

    # Store references
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_MQTT_CLIENT: mqtt_client,
        DATA_PUBLISHER: publisher,
        DATA_SUBSCRIBER: subscriber,
    }

    # Connect to MQTT
    try:
        await mqtt_client.async_connect()
    except Exception:
        _LOGGER.exception("Failed to connect to MQTT broker %s", data[CONF_BROKER_HOST])
        # Don't fail setup - reconnect will happen in background
        _LOGGER.warning("Will retry MQTT connection in background")

    # Set up platforms (this triggers async_setup_entry in each platform file)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start publisher and subscriber after platforms are loaded
    if mqtt_client.connected:
        await publisher.async_start()
        await subscriber.async_start()
    else:
        # Start when connected
        async def _start_when_connected():
            """Start publisher/subscriber once MQTT connects."""
            import asyncio
            # Wait for connection (with timeout)
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


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id, {})

        # Stop publisher and subscriber
        publisher: Publisher | None = entry_data.get(DATA_PUBLISHER)
        subscriber: Subscriber | None = entry_data.get(DATA_SUBSCRIBER)
        mqtt_client: MQTTClient | None = entry_data.get(DATA_MQTT_CLIENT)

        if publisher:
            await publisher.async_stop()
        if subscriber:
            await subscriber.async_stop()
        if mqtt_client:
            await mqtt_client.async_disconnect()

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
    publisher: Publisher | None = entry_data.get(DATA_PUBLISHER)

    if publisher:
        new_data = entry.data
        await publisher.async_update_selection(
            selected_devices=new_data.get("selected_devices", []),
            selected_entities=new_data.get("selected_entities", []),
        )
