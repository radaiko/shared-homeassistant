"""Shared Home Assistant v2 — share devices between HA instances via MQTT Discovery."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
    DOMAIN,
    PAYLOAD_OFFLINE,
    TOPIC_BRIDGE_AVAILABILITY,
    SharedHARuntimeData,
)
from .history_sync import HistoryConsumer, HistoryProvider
from .mqtt_client import MQTTClient
from .publisher import Publisher

_LOGGER = logging.getLogger(__name__)

type SharedHAConfigEntry = ConfigEntry[SharedHARuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: SharedHAConfigEntry) -> bool:
    """Set up Shared Home Assistant v2 from a config entry."""
    data = entry.data
    instance_id = data[CONF_INSTANCE_ID]
    instance_name = data[CONF_INSTANCE_NAME]

    own_mqtt = MQTTClient(
        host=data[CONF_OWN_BROKER_HOST],
        port=int(data[CONF_OWN_BROKER_PORT]),
        instance_id=f"{instance_id}_own",
        instance_name=instance_name,
        username=data.get(CONF_OWN_BROKER_USERNAME) or None,
        password=data.get(CONF_OWN_BROKER_PASSWORD) or None,
        use_tls=data.get(CONF_OWN_BROKER_TLS, False),
    )

    peer_mqtt = MQTTClient(
        host=data[CONF_PEER_BROKER_HOST],
        port=int(data[CONF_PEER_BROKER_PORT]),
        instance_id=f"{instance_id}_peer",
        instance_name=instance_name,
        username=data.get(CONF_PEER_BROKER_USERNAME) or None,
        password=data.get(CONF_PEER_BROKER_PASSWORD) or None,
        use_tls=data.get(CONF_PEER_BROKER_TLS, False),
        will_topic=TOPIC_BRIDGE_AVAILABILITY.format(instance_id=instance_id),
        will_payload=PAYLOAD_OFFLINE,
    )

    publisher = Publisher(hass, peer_mqtt, dict(data))
    history_provider = HistoryProvider(hass, own_mqtt, instance_id)
    history_consumer = HistoryConsumer(hass, peer_mqtt, instance_id)

    entry.runtime_data = SharedHARuntimeData(
        own_mqtt=own_mqtt,
        peer_mqtt=peer_mqtt,
        publisher=publisher,
        history_provider=history_provider,
        history_consumer=history_consumer,
    )

    # Register MQTT subscriptions before connect so they resubscribe on reconnect
    await history_provider.async_register_subscriptions()
    await history_consumer.async_register_subscriptions()

    # Connect both brokers in parallel (they're independent)
    results = await asyncio.gather(
        _safe_connect(own_mqtt, "own"),
        _safe_connect(peer_mqtt, "peer"),
        return_exceptions=False,
    )
    own_ok, peer_ok = results

    async def _start_when_ready() -> None:
        # Publisher depends on peer broker connection; history_provider on own
        if peer_ok:
            try:
                await publisher.async_start()
                await history_consumer.async_start()
            except Exception:
                _LOGGER.exception("Failed to start peer-side components")
        else:
            hass.async_create_task(_wait_and_start_peer(peer_mqtt, publisher, history_consumer))

        if own_ok:
            try:
                await history_provider.async_start()
            except Exception:
                _LOGGER.exception("Failed to start own-side components")
        else:
            hass.async_create_task(_wait_and_start_own(own_mqtt, history_provider))

    hass.async_create_task(_start_when_ready())

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _safe_connect(mqtt: MQTTClient, label: str) -> bool:
    try:
        await mqtt.async_connect()
        return True
    except Exception:
        _LOGGER.exception("Failed to connect to %s broker; will retry in background", label)
        return False


async def _wait_and_start_peer(
    mqtt: MQTTClient, publisher: Publisher, consumer: HistoryConsumer
) -> None:
    try:
        await asyncio.wait_for(mqtt._connected.wait(), timeout=600)
        await publisher.async_start()
        await consumer.async_start()
    except asyncio.TimeoutError:
        _LOGGER.warning("Timed out waiting for peer broker; components not started")


async def _wait_and_start_own(mqtt: MQTTClient, provider: HistoryProvider) -> None:
    try:
        await asyncio.wait_for(mqtt._connected.wait(), timeout=600)
        await provider.async_start()
    except asyncio.TimeoutError:
        _LOGGER.warning("Timed out waiting for own broker; history_provider not started")


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle config entry migration.

    v2 has a fundamentally different schema (dual-broker + MQTT Discovery).
    There's no clean data migration from v1 — the user must remove the old
    config entry and add a fresh one.
    """
    if entry.version < 2:
        _LOGGER.error(
            "Cannot migrate v1 Shared HA config entry to v2: topology and "
            "data schema are incompatible. Please remove this config entry "
            "and re-add the integration with the v2 flow."
        )
        return False
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SharedHAConfigEntry) -> bool:
    rt = entry.runtime_data
    try:
        await rt.publisher.async_stop()
    except Exception:
        _LOGGER.exception("publisher stop failed")
    try:
        await rt.history_consumer.async_stop()
    except Exception:
        _LOGGER.exception("history_consumer stop failed")
    try:
        await rt.history_provider.async_stop()
    except Exception:
        _LOGGER.exception("history_provider stop failed")

    await asyncio.gather(
        rt.peer_mqtt.async_disconnect(),
        rt.own_mqtt.async_disconnect(),
        return_exceptions=True,
    )
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: SharedHAConfigEntry
) -> None:
    rt = entry.runtime_data
    await rt.publisher.async_update_selection(dict(entry.data))
