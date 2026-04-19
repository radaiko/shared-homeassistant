"""Constants for the Shared Home Assistant integration (v2)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .history_sync import HistoryConsumer, HistoryProvider
    from .mqtt_client import MQTTClient
    from .publisher import Publisher

DOMAIN = "shared_homeassistant"

# ---------------------------------------------------------------------------
# Topic schema (v2)
# ---------------------------------------------------------------------------
# Topology: each HA runs its own broker. Shares are published to the PEER's
# broker so the peer's native MQTT integration discovers them there, while the
# local native MQTT integration never sees our own shared discoveries (no
# self-duplication).
#
# Peer-broker topics (we publish, peer's HA/MQTT consumes):
#   homeassistant/<domain>/shared_ha_<iid>_<uid>/config   MQTT Discovery config
#   shared_ha/<iid>/bridge/availability                    LWT online/offline
#   shared_ha/<iid>/e/<entity_id>/state                    state (retained)
#   shared_ha/<iid>/e/<entity_id>/attr                     JSON attrs (retained)
#   shared_ha/<iid>/e/<entity_id>/set                      inbound commands
#
# Own-broker topics (we subscribe, peers publish here to query us):
#   shared_ha/<iid>/history_request/<entity_id>            peer -> us
#   shared_ha/<iid>/history_response/<req_id>/<eid>/<idx>  us -> peer (chunks)
#   shared_ha/<iid>/history_response/<req_id>/<eid>/done   us -> peer (done)
# ---------------------------------------------------------------------------

TOPIC_PREFIX = "shared_ha"
DISCOVERY_PREFIX_DEFAULT = "homeassistant"  # peer's native MQTT discovery prefix

# Availability / bridge
TOPIC_BRIDGE_AVAILABILITY = f"{TOPIC_PREFIX}/{{instance_id}}/bridge/availability"
PAYLOAD_ONLINE = "online"
PAYLOAD_OFFLINE = "offline"

# Per-entity topics (peer broker)
TOPIC_ENTITY_STATE = f"{TOPIC_PREFIX}/{{instance_id}}/e/{{entity_id}}/state"
TOPIC_ENTITY_ATTR = f"{TOPIC_PREFIX}/{{instance_id}}/e/{{entity_id}}/attr"
TOPIC_ENTITY_SET = f"{TOPIC_PREFIX}/{{instance_id}}/e/{{entity_id}}/set"
TOPIC_ENTITY_SET_WILDCARD = f"{TOPIC_PREFIX}/{{instance_id}}/e/+/set"

# Discovery config topic (peer broker)
TOPIC_DISCOVERY_CONFIG = "{discovery_prefix}/{component}/{object_id}/config"

# History topics
TOPIC_HISTORY_REQUEST = f"{TOPIC_PREFIX}/{{instance_id}}/history_request/{{entity_id}}"
TOPIC_HISTORY_REQUEST_WILDCARD = f"{TOPIC_PREFIX}/{{instance_id}}/history_request/#"
TOPIC_HISTORY_CHUNK = (
    f"{TOPIC_PREFIX}/{{instance_id}}/history_response/"
    "{requesting_id}/{entity_id}/{chunk_idx}"
)
TOPIC_HISTORY_DONE = (
    f"{TOPIC_PREFIX}/{{instance_id}}/history_response/"
    "{requesting_id}/{entity_id}/done"
)
TOPIC_HISTORY_RESPONSE_WILDCARD = (
    f"{TOPIC_PREFIX}/+/history_response/{{instance_id}}/#"
)

# Watch peer's discovery to know which entities are shared from them (for
# automatic history sync). Pattern matches our own bridge's publications on
# the local broker.
TOPIC_DISCOVERY_WATCH = "{discovery_prefix}/+/shared_ha_+/config"

# ---------------------------------------------------------------------------
# Config keys
# ---------------------------------------------------------------------------

# Own broker (runs locally, hosts our history_provider endpoint)
CONF_OWN_BROKER_HOST = "own_broker_host"
CONF_OWN_BROKER_PORT = "own_broker_port"
CONF_OWN_BROKER_USERNAME = "own_broker_username"
CONF_OWN_BROKER_PASSWORD = "own_broker_password"
CONF_OWN_BROKER_TLS = "own_broker_tls"

# Peer broker (remote; where we publish our shares for the peer to consume)
CONF_PEER_BROKER_HOST = "peer_broker_host"
CONF_PEER_BROKER_PORT = "peer_broker_port"
CONF_PEER_BROKER_USERNAME = "peer_broker_username"
CONF_PEER_BROKER_PASSWORD = "peer_broker_password"
CONF_PEER_BROKER_TLS = "peer_broker_tls"
CONF_PEER_DISCOVERY_PREFIX = "peer_discovery_prefix"

# Instance identity
CONF_INSTANCE_NAME = "instance_name"
CONF_INSTANCE_ID = "instance_id"

# Share selection
CONF_SHARED_INTEGRATIONS = "shared_integrations"   # list of config_entry_ids
CONF_SHARED_DEVICES = "shared_devices"             # list of device_ids
CONF_SHARED_ENTITIES = "shared_entities"           # list of entity_ids
CONF_READONLY_DEVICES = "readonly_devices"         # subset of shared_devices
CONF_READONLY_ENTITIES = "readonly_entities"       # subset of shared_entities
CONF_READONLY_INTEGRATIONS = "readonly_integrations"  # subset of shared_integrations

# Defaults
DEFAULT_BROKER_PORT = 1883
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
HISTORY_CHUNK_SIZE = 100

# Supported domains — determines which entities we share and how we publish
# their discovery configs / handle commands.
SUPPORTED_DOMAINS = frozenset(
    {
        "sensor",
        "binary_sensor",
        "switch",
        "light",
        "cover",
        "number",
        "lock",
        "input_boolean",
        "input_number",
    }
)

# Domains that accept commands (render a command_topic in discovery).
SETTABLE_DOMAINS = frozenset(
    {
        "switch",
        "light",
        "cover",
        "number",
        "lock",
        "input_boolean",
        "input_number",
    }
)


@dataclass
class SharedHARuntimeData:
    """Runtime data attached to the config entry."""

    own_mqtt: "MQTTClient"
    peer_mqtt: "MQTTClient"
    publisher: "Publisher"
    history_provider: "HistoryProvider"
    history_consumer: "HistoryConsumer"
