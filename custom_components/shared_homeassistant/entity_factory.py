"""Entity factory for Shared Home Assistant - creates entities based on domain."""

from __future__ import annotations

import logging
from typing import Any

from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)


def create_entity(
    domain: str,
    entity_data: dict[str, Any],
    instance_id: str,
    instance_name: str,
    device_id: str,
    mqtt_client: MQTTClient,
    entity_prefix: str,
) -> Any | None:
    """Create a shared entity based on domain."""
    from .sensor import SharedSensor
    from .binary_sensor import SharedBinarySensor
    from .switch import SharedSwitch
    from .light import SharedLight
    from .cover import SharedCover
    from .climate import SharedClimate
    from .number import SharedNumber

    factory_map = {
        "sensor": SharedSensor,
        "binary_sensor": SharedBinarySensor,
        "switch": SharedSwitch,
        "light": SharedLight,
        "cover": SharedCover,
        "climate": SharedClimate,
        "number": SharedNumber,
    }

    entity_class = factory_map.get(domain)
    if entity_class is None:
        _LOGGER.warning("No entity class for domain %s", domain)
        return None

    return entity_class(
        entity_data=entity_data,
        instance_id=instance_id,
        instance_name=instance_name,
        device_id=device_id,
        mqtt_client=mqtt_client,
        entity_prefix=entity_prefix,
    )
