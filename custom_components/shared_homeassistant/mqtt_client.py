"""MQTT client wrapper for Shared Home Assistant."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
from collections.abc import Callable, Coroutine
from typing import Any

import aiomqtt

from .const import TOPIC_HEARTBEAT

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff settings
_MIN_RECONNECT_DELAY = 1
_MAX_RECONNECT_DELAY = 300


class MQTTClient:
    """Async MQTT client with auto-reconnect and LWT support."""

    def __init__(
        self,
        host: str,
        port: int,
        instance_id: str,
        instance_name: str,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = False,
    ) -> None:
        """Initialize the MQTT client."""
        self._host = host
        self._port = port
        self._instance_id = instance_id
        self._instance_name = instance_name
        self._username = username
        self._password = password
        self._use_tls = use_tls

        self._client: aiomqtt.Client | None = None
        self._listen_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._connected = asyncio.Event()
        self._shutdown = False

        self._subscriptions: dict[str, Callable[[str, bytes], Coroutine]] = {}

        # Last Will and Testament
        self._will_topic = TOPIC_HEARTBEAT.format(instance_id=instance_id)
        self._will_payload = json.dumps(
            {"online": False, "instance_name": instance_name}
        )

    @property
    def connected(self) -> bool:
        """Return True if connected."""
        return self._connected.is_set()

    async def async_connect(self) -> None:
        """Connect to the MQTT broker."""
        self._shutdown = False
        await self._do_connect()

    async def _do_connect(self) -> None:
        """Perform the actual connection."""
        # Clean up existing client if reconnecting
        if self._client is not None:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None

        tls_params = None
        if self._use_tls:
            tls_context = ssl.create_default_context()
            tls_context.check_hostname = False
            tls_context.verify_mode = ssl.CERT_NONE
            tls_params = tls_context

        will = aiomqtt.Will(
            topic=self._will_topic,
            payload=self._will_payload.encode(),
            qos=1,
            retain=True,
        )

        self._client = aiomqtt.Client(
            hostname=self._host,
            port=self._port,
            username=self._username or None,
            password=self._password or None,
            tls_context=tls_params,
            will=will,
            identifier=f"shared_ha_{self._instance_id}",
        )

        await self._client.__aenter__()
        self._connected.set()
        _LOGGER.info(
            "Connected to MQTT broker %s:%s as %s",
            self._host,
            self._port,
            self._instance_id,
        )

        # Publish online heartbeat
        await self.async_publish(
            self._will_topic,
            json.dumps({"online": True, "instance_name": self._instance_name}),
            retain=True,
        )

        # Resubscribe to all topics
        for topic in self._subscriptions:
            await self._client.subscribe(topic)

        # Start listening for messages
        self._listen_task = asyncio.create_task(self._listen())

    async def _listen(self) -> None:
        """Listen for incoming messages."""
        try:
            async for message in self._client.messages:
                topic = str(message.topic)
                payload = message.payload
                if isinstance(payload, (bytes, bytearray)):
                    payload = bytes(payload)
                else:
                    payload = str(payload).encode()

                for pattern, callback in self._subscriptions.items():
                    if _topic_matches(pattern, topic):
                        try:
                            await callback(topic, payload)
                        except Exception:
                            _LOGGER.exception(
                                "Error in MQTT callback for topic %s", topic
                            )
        except aiomqtt.MqttError as err:
            self._connected.clear()
            if not self._shutdown:
                _LOGGER.warning("MQTT connection lost: %s", err)
                self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnect with exponential backoff."""
        delay = _MIN_RECONNECT_DELAY
        while not self._shutdown:
            _LOGGER.info("Attempting MQTT reconnect in %s seconds...", delay)
            await asyncio.sleep(delay)
            try:
                await self._do_connect()
                _LOGGER.info("MQTT reconnected successfully")
                return
            except Exception:
                _LOGGER.warning("MQTT reconnect failed, retrying...")
                delay = min(delay * 2, _MAX_RECONNECT_DELAY)

    async def async_disconnect(self) -> None:
        """Disconnect from the MQTT broker."""
        self._shutdown = True
        self._connected.clear()

        if self._listen_task and not self._listen_task.done():
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass

        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass

        if self._client:
            # Publish offline heartbeat before disconnecting
            try:
                await self.async_publish(
                    self._will_topic,
                    json.dumps(
                        {"online": False, "instance_name": self._instance_name}
                    ),
                    retain=True,
                )
            except Exception:
                pass
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
            self._client = None

    async def async_publish(
        self, topic: str, payload: str, retain: bool = False, qos: int = 1
    ) -> None:
        """Publish a message."""
        if not self._connected.is_set():
            _LOGGER.warning("Cannot publish to %s: not connected", topic)
            return
        await self._client.publish(topic, payload.encode(), qos=qos, retain=retain)

    async def async_subscribe(
        self,
        topic: str,
        callback: Callable[[str, bytes], Coroutine],
    ) -> None:
        """Subscribe to a topic pattern."""
        self._subscriptions[topic] = callback
        if self._connected.is_set() and self._client:
            await self._client.subscribe(topic)

    async def async_unsubscribe(self, topic: str) -> None:
        """Unsubscribe from a topic pattern."""
        self._subscriptions.pop(topic, None)
        if self._connected.is_set() and self._client:
            try:
                await self._client.unsubscribe(topic)
            except Exception:
                pass


def _topic_matches(pattern: str, topic: str) -> bool:
    """Check if an MQTT topic matches a subscription pattern."""
    pattern_parts = pattern.split("/")
    topic_parts = topic.split("/")

    i = 0
    for i, part in enumerate(pattern_parts):
        if part == "#":
            return True
        if i >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[i]:
            return False

    return len(pattern_parts) == len(topic_parts)
