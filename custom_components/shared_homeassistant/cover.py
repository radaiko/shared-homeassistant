"""Cover platform for Shared Home Assistant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
    ATTR_POSITION,
    ATTR_TILT_POSITION,
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
    """Set up shared cover entities."""
    subscriber = config_entry.runtime_data.subscriber
    subscriber.register_platform("cover", async_add_entities)

    catch_up = subscriber.get_entities_for_domain("cover")
    if catch_up:
        async_add_entities(catch_up)


class SharedCover(SharedBaseEntity, CoverEntity):
    """A shared cover entity."""

    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, **kwargs: Any) -> None:
        """Initialize the shared cover."""
        super().__init__(**kwargs)
        self._position: int | None = None

    @property
    def is_closed(self) -> bool | None:
        """Return true if the cover is closed."""
        if self._remote_state is None:
            return None
        return self._remote_state == "closed"

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position of the cover."""
        return self._position

    def _process_state_update(
        self, state: str | None, attributes: dict[str, Any]
    ) -> None:
        """Process cover state update."""
        self._position = attributes.get("current_position")

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        await self._async_send_command("cover.open_cover")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        await self._async_send_command("cover.close_cover")

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the cover."""
        await self._async_send_command("cover.stop_cover")

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Set the cover position."""
        position = kwargs.get(ATTR_POSITION)
        if position is not None:
            await self._async_send_command(
                "cover.set_cover_position", {"position": position}
            )
