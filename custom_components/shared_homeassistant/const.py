"""Constants for the Shared Home Assistant integration."""

DOMAIN = "shared_homeassistant"

# MQTT topic templates
TOPIC_PREFIX = "shared_ha"
TOPIC_DEVICE = f"{TOPIC_PREFIX}/{{instance_id}}/devices/{{device_id}}"
TOPIC_STATE = f"{TOPIC_PREFIX}/{{instance_id}}/states/{{entity_id}}"
TOPIC_COMMAND = f"{TOPIC_PREFIX}/{{instance_id}}/commands/{{entity_id}}"
TOPIC_HEARTBEAT = f"{TOPIC_PREFIX}/{{instance_id}}/heartbeat"

# Subscription wildcards
TOPIC_SUB_DEVICES = f"{TOPIC_PREFIX}/+/devices/#"
TOPIC_SUB_STATES = f"{TOPIC_PREFIX}/+/states/#"
TOPIC_SUB_COMMANDS = f"{TOPIC_PREFIX}/+/commands/#"

# Config keys
CONF_BROKER_HOST = "broker_host"
CONF_BROKER_PORT = "broker_port"
CONF_BROKER_USERNAME = "broker_username"
CONF_BROKER_PASSWORD = "broker_password"
CONF_USE_TLS = "use_tls"
CONF_INSTANCE_NAME = "instance_name"
CONF_INSTANCE_ID = "instance_id"
CONF_SELECTED_DEVICES = "selected_devices"
CONF_SELECTED_ENTITIES = "selected_entities"
CONF_ENTITY_PREFIX = "entity_prefix"

# Defaults
DEFAULT_PORT = 1883
DEFAULT_ENTITY_PREFIX = ""

# Supported platforms for shared entities
PLATFORMS = [
    "sensor",
    "binary_sensor",
    "switch",
    "light",
    "cover",
    "climate",
    "number",
]

# Data keys stored in hass.data[DOMAIN]
DATA_MQTT_CLIENT = "mqtt_client"
DATA_PUBLISHER = "publisher"
DATA_SUBSCRIBER = "subscriber"
DATA_UNSUBSCRIBE_CALLBACKS = "unsubscribe_callbacks"
