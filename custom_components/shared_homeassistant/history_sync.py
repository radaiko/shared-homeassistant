"""History synchronization for Shared Home Assistant."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    TOPIC_PREFIX,
    TOPIC_HISTORY_CHUNK,
    TOPIC_HISTORY_DONE,
    CONF_INSTANCE_ID,
    HISTORY_CHUNK_SIZE,
)
from .mqtt_client import MQTTClient

_LOGGER = logging.getLogger(__name__)

# Storage key for tracking last imported timestamps
STORAGE_KEY = "shared_homeassistant_history"
STORAGE_VERSION = 1


class HistoryProvider:
    """Responds to history requests from other instances (runs on source)."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: MQTTClient,
        instance_id: str,
    ) -> None:
        """Initialize the history provider."""
        self._hass = hass
        self._mqtt = mqtt_client
        self._instance_id = instance_id

    async def async_start(self) -> None:
        """Start listening for history requests."""
        topic = f"{TOPIC_PREFIX}/{self._instance_id}/history_request/#"
        await self._mqtt.async_subscribe(topic, self._handle_history_request)

    async def async_stop(self) -> None:
        """Stop listening."""
        topic = f"{TOPIC_PREFIX}/{self._instance_id}/history_request/#"
        await self._mqtt.async_unsubscribe(topic)

    async def _handle_history_request(self, topic: str, payload: bytes) -> None:
        """Handle incoming history request from a subscriber."""
        if not payload:
            return

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("Invalid history request on topic %s", topic)
            return

        requesting_id = data.get("requesting_instance")
        entity_id = data.get("entity_id")
        since = data.get("since")  # ISO timestamp or None for full history

        if not requesting_id or not entity_id:
            return

        _LOGGER.info(
            "History request from %s for %s (since=%s)",
            requesting_id,
            entity_id,
            since or "all-time",
        )

        await self._send_history(requesting_id, entity_id, since)

    async def _send_history(
        self, requesting_id: str, entity_id: str, since: str | None
    ) -> None:
        """Query local statistics and send them as chunks."""
        try:
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )
            from homeassistant.helpers.recorder import get_instance
        except ImportError:
            _LOGGER.error("Recorder component not available, cannot provide history")
            return

        # Parse since timestamp
        start_time = None
        if since:
            try:
                start_time = datetime.fromisoformat(since)
            except ValueError:
                _LOGGER.warning("Invalid 'since' timestamp: %s", since)

        if start_time is None:
            # Default: all time (use a very old date)
            start_time = datetime(2000, 1, 1, tzinfo=UTC)

        # Query statistics from the recorder
        try:
            stats = await get_instance(self._hass).async_add_executor_job(
                lambda: statistics_during_period(
                    self._hass,
                    start_time,
                    None,  # end_time = now
                    {entity_id},
                    "hour",
                    None,  # units
                    {"start", "state", "mean", "min", "max", "sum", "last_reset"},
                )
            )
        except Exception:
            _LOGGER.exception("Failed to query statistics for %s", entity_id)
            return

        entity_stats = stats.get(entity_id, [])

        if not entity_stats:
            _LOGGER.debug("No statistics found for %s since %s", entity_id, since)
            # Still send done signal so subscriber knows
            await self._send_done(requesting_id, entity_id, 0)
            return

        # Also need metadata for the subscriber to import
        try:
            from homeassistant.components.recorder.statistics import get_metadata

            metadata_result = await get_instance(self._hass).async_add_executor_job(
                lambda: get_metadata(self._hass, statistic_ids={entity_id})
            )
        except Exception:
            _LOGGER.exception("Failed to get metadata for %s", entity_id)
            return

        metadata = None
        for _id, meta in metadata_result.items():
            # get_metadata returns {id: (statistic_id, meta_dict)} or {id: meta_dict}
            if isinstance(meta, tuple):
                metadata = meta[1] if len(meta) > 1 else meta[0]
            else:
                metadata = meta
            break

        if metadata is None:
            _LOGGER.warning("No statistics metadata found for %s", entity_id)
            await self._send_done(requesting_id, entity_id, 0)
            return

        # Serialize metadata - handle both dict and object access patterns
        def _get(obj, key, default=None):
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        meta_dict = {
            "statistic_id": _get(metadata, "statistic_id", entity_id),
            "source": _get(metadata, "source", "recorder"),
            "name": _get(metadata, "name"),
            "unit_of_measurement": _get(metadata, "unit_of_measurement"),
            "has_sum": _get(metadata, "has_sum", False),
            "mean_type": _get(metadata, "mean_type", 0),
            "unit_class": _get(metadata, "unit_class"),
        }

        # Chunk and send
        chunk_idx = 0
        for i in range(0, len(entity_stats), HISTORY_CHUNK_SIZE):
            chunk = entity_stats[i : i + HISTORY_CHUNK_SIZE]

            # Serialize each stat row
            serialized = []
            for row in chunk:
                stat_row = {"start": row["start"]}
                for key in ("state", "sum", "mean", "min", "max", "last_reset"):
                    if key in row and row[key] is not None:
                        val = row[key]
                        # Convert datetime to ISO string
                        if isinstance(val, datetime):
                            stat_row[key] = val.isoformat()
                        elif isinstance(val, (int, float)):
                            # start is returned as epoch ms from WS, but
                            # statistics_during_period returns datetime objects
                            stat_row[key] = val
                        else:
                            stat_row[key] = val
                serialized.append(stat_row)

            payload = {
                "metadata": meta_dict,
                "stats": serialized,
                "chunk_idx": chunk_idx,
                "entity_id": entity_id,
            }

            topic = TOPIC_HISTORY_CHUNK.format(
                instance_id=self._instance_id,
                requesting_id=requesting_id,
                entity_id=entity_id,
                chunk_idx=chunk_idx,
            )
            await self._mqtt.async_publish(topic, json.dumps(payload), retain=False)
            chunk_idx += 1

        await self._send_done(requesting_id, entity_id, chunk_idx)
        _LOGGER.info(
            "Sent %d history chunks (%d rows) for %s to %s",
            chunk_idx,
            len(entity_stats),
            entity_id,
            requesting_id,
        )

    async def _send_done(
        self, requesting_id: str, entity_id: str, total_chunks: int
    ) -> None:
        """Send completion signal."""
        topic = TOPIC_HISTORY_DONE.format(
            instance_id=self._instance_id,
            requesting_id=requesting_id,
            entity_id=entity_id,
        )
        payload = {
            "entity_id": entity_id,
            "total_chunks": total_chunks,
        }
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=False)


class HistoryConsumer:
    """Requests and imports history from source instances (runs on subscriber)."""

    def __init__(
        self,
        hass: HomeAssistant,
        mqtt_client: MQTTClient,
        instance_id: str,
    ) -> None:
        """Initialize the history consumer."""
        self._hass = hass
        self._mqtt = mqtt_client
        self._instance_id = instance_id
        # {entity_id: ISO timestamp} — last imported stat per entity
        self._last_imported: dict[str, str] = {}
        # Buffer for incoming chunks: {(source_id, entity_id): [chunk_payloads]}
        self._chunk_buffer: dict[tuple[str, str], list[dict]] = {}
        # Track metadata per entity from first chunk
        self._metadata_buffer: dict[tuple[str, str], dict] = {}

    async def async_start(self) -> None:
        """Start listening for history responses."""
        # Subscribe to responses directed at us
        topic = f"{TOPIC_PREFIX}/+/history_response/{self._instance_id}/#"
        await self._mqtt.async_subscribe(topic, self._handle_history_response)

        # Load last imported timestamps from storage
        await self._load_state()

    async def async_stop(self) -> None:
        """Stop listening."""
        topic = f"{TOPIC_PREFIX}/+/history_response/{self._instance_id}/#"
        await self._mqtt.async_unsubscribe(topic)

    async def async_request_history(
        self, source_instance_id: str, entity_id: str
    ) -> None:
        """Request history for an entity from its source instance."""
        since = self._last_imported.get(entity_id)

        payload = {
            "requesting_instance": self._instance_id,
            "entity_id": entity_id,
            "since": since,
        }

        topic = f"{TOPIC_PREFIX}/{source_instance_id}/history_request/{entity_id}"
        await self._mqtt.async_publish(topic, json.dumps(payload), retain=False)
        _LOGGER.info(
            "Requested history for %s from %s (since=%s)",
            entity_id,
            source_instance_id,
            since or "all-time",
        )

    async def _handle_history_response(self, topic: str, payload: bytes) -> None:
        """Handle incoming history chunks or done signals."""
        if not payload:
            return

        # Topic format: shared_ha/{source_id}/history_response/{our_id}/{entity_id}/{chunk_idx_or_done}
        parts = topic.split("/")
        if len(parts) < 6:
            return

        source_id = parts[1]
        # parts[2] = "history_response", parts[3] = our instance_id
        entity_id = parts[4]
        suffix = parts[5] if len(parts) > 5 else ""

        try:
            data = json.loads(payload.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            _LOGGER.warning("Invalid history response on topic %s", topic)
            return

        key = (source_id, entity_id)

        if suffix == "done":
            await self._finalize_import(key, data)
        else:
            # It's a chunk
            if key not in self._chunk_buffer:
                self._chunk_buffer[key] = []
            self._chunk_buffer[key].extend(data.get("stats", []))

            # Store metadata from first chunk
            if key not in self._metadata_buffer and "metadata" in data:
                self._metadata_buffer[key] = data["metadata"]

    async def _finalize_import(
        self, key: tuple[str, str], done_data: dict
    ) -> None:
        """Import all buffered chunks for an entity."""
        source_id, entity_id = key
        total_chunks = done_data.get("total_chunks", 0)

        stats_rows = self._chunk_buffer.pop(key, [])
        metadata = self._metadata_buffer.pop(key, None)

        if not stats_rows or not metadata:
            _LOGGER.debug(
                "No history data to import for %s from %s (%d chunks)",
                entity_id,
                source_id,
                total_chunks,
            )
            return

        try:
            from homeassistant.components.recorder.statistics import (
                async_import_statistics,
                async_add_external_statistics,
            )
            from homeassistant.components.recorder.models import StatisticMeanType
            from homeassistant.helpers.recorder import get_instance
        except ImportError:
            _LOGGER.error("Recorder component not available, cannot import history")
            return

        # Build StatisticMetaData dict
        # Use the shared entity's local statistic_id (which may be prefixed)
        # For now, use the original entity_id as statistic_id
        source_str = metadata.get("source", "recorder")

        mean_type_val = metadata.get("mean_type", 0)
        try:
            mean_type = StatisticMeanType(mean_type_val)
        except ValueError:
            mean_type = StatisticMeanType.NONE

        meta_dict = {
            "statistic_id": entity_id,
            "source": source_str,
            "name": metadata.get("name"),
            "unit_of_measurement": metadata.get("unit_of_measurement"),
            "has_sum": metadata.get("has_sum", False),
            "mean_type": mean_type,
            "unit_class": metadata.get("unit_class"),
        }

        # Build StatisticData list
        stat_list = []
        last_start = None
        for row in stats_rows:
            start_val = row.get("start")
            if start_val is None:
                continue

            # Parse start timestamp
            if isinstance(start_val, str):
                try:
                    start_dt = datetime.fromisoformat(start_val)
                except ValueError:
                    continue
            elif isinstance(start_val, (int, float)):
                # Epoch seconds or milliseconds
                if start_val > 1e12:
                    start_dt = datetime.fromtimestamp(start_val / 1000, tz=UTC)
                else:
                    start_dt = datetime.fromtimestamp(start_val, tz=UTC)
            else:
                continue

            stat_data: dict[str, Any] = {"start": start_dt}

            for fkey in ("state", "sum", "mean", "min", "max"):
                if fkey in row and row[fkey] is not None:
                    stat_data[fkey] = float(row[fkey])

            if "last_reset" in row and row["last_reset"] is not None:
                lr = row["last_reset"]
                if isinstance(lr, str):
                    try:
                        stat_data["last_reset"] = datetime.fromisoformat(lr)
                    except ValueError:
                        pass
                elif isinstance(lr, (int, float)):
                    if lr > 1e12:
                        stat_data["last_reset"] = datetime.fromtimestamp(
                            lr / 1000, tz=UTC
                        )
                    else:
                        stat_data["last_reset"] = datetime.fromtimestamp(lr, tz=UTC)

            stat_list.append(stat_data)
            last_start = start_dt

        if not stat_list:
            _LOGGER.debug("No valid statistics rows for %s", entity_id)
            return

        # Import into recorder
        try:
            if source_str == "recorder":
                async_import_statistics(self._hass, meta_dict, stat_list)
            else:
                async_add_external_statistics(self._hass, meta_dict, stat_list)

            # Wait for recorder to process
            await get_instance(self._hass).async_block_till_done()

            _LOGGER.info(
                "Imported %d statistics rows for %s from instance %s",
                len(stat_list),
                entity_id,
                source_id,
            )
        except Exception:
            _LOGGER.exception("Failed to import statistics for %s", entity_id)
            return

        # Update last imported timestamp
        if last_start:
            self._last_imported[entity_id] = last_start.isoformat()
            await self._save_state()

    async def _load_state(self) -> None:
        """Load last imported timestamps from HA storage."""
        from homeassistant.helpers.storage import Store

        store = Store(self._hass, STORAGE_VERSION, STORAGE_KEY)
        data = await store.async_load()
        if data and isinstance(data, dict):
            self._last_imported = data.get("last_imported", {})

    async def _save_state(self) -> None:
        """Save last imported timestamps to HA storage."""
        from homeassistant.helpers.storage import Store

        store = Store(self._hass, STORAGE_VERSION, STORAGE_KEY)
        await store.async_save({"last_imported": self._last_imported})
