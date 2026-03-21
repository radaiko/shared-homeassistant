# Shared Home Assistant

Share devices and entities between multiple Home Assistant instances using a central MQTT broker.

## What it does

- **Share devices and entities** from one HA instance to any number of other HA instances
- **Bidirectional control**: shared switches, lights, covers, and climate entities can be controlled from any instance — commands are forwarded back to the origin instance via MQTT
- **Real-time state sync**: state changes are published instantly via MQTT with retained messages, so new instances receive the current state immediately on connect
- **Automatic availability tracking**: each instance publishes a heartbeat with MQTT Last Will — if an instance goes offline, its shared entities show as "unavailable" on all other instances
- **Independent MQTT connection**: uses its own MQTT broker connection (via `aiomqtt`), separate from your existing HA MQTT integration — so your shared broker can be different from your local one

## Supported entity types

| Domain | Behavior |
|---|---|
| `sensor` | Read-only state mirror |
| `binary_sensor` | Read-only state mirror |
| `switch` | State mirror + turn_on / turn_off / toggle |
| `light` | State mirror + turn_on (brightness, color_temp_kelvin, rgb) / turn_off / toggle |
| `cover` | State mirror + open / close / stop / set_position |
| `climate` | State mirror + set_temperature / set_hvac_mode / set_fan_mode |
| `number` | State mirror + set_value |

## What it does NOT do

- **No historical data sync** — only the current state is shared via MQTT retain. History builds up locally on each instance from the moment the shared entity appears.
- **No dashboard sync** — planned for a future version. Custom dashboards referencing shared entities must be created manually on each instance.
- **No automatic HACS card installation** — if you share entities that use custom frontend cards, those cards must be installed manually on each receiving instance.
- **No conflict resolution** — if two instances share entities with identical IDs, behavior is undefined.
- **No TLS certificate verification** — TLS is supported but certificate validation is disabled (accepts any certificate).

## Requirements

- Home Assistant **2026.3** or newer
- An MQTT broker accessible by all instances (e.g. [Mosquitto](https://mosquitto.org/))
- The MQTT broker does **not** need to be the same one used by your HA MQTT integration

## Installation

### Via HACS (recommended)

1. Open HACS in your Home Assistant UI
2. Click the three-dot menu (top right) → **Custom repositories**
3. Add `https://github.com/radaiko/shared-homeassistant` with type **Integration**
4. Click **Download** on the "Shared Home Assistant" integration
5. **Restart Home Assistant**
6. Repeat on every HA instance you want to connect

### Manual installation

1. Copy the `custom_components/shared_homeassistant/` folder into your HA's `custom_components/` directory
2. Restart Home Assistant

## Setup

### Step 1 — MQTT Broker

Go to **Settings → Integrations → Add Integration** and search for "Shared Home Assistant".

Configure your shared MQTT broker:

| Field | Description |
|---|---|
| Broker Host | IP address or hostname of the MQTT broker |
| Broker Port | Default: `1883` |
| Username | Optional — leave empty if no authentication |
| Password | Optional |
| Use TLS | Enable if your broker requires TLS |

### Step 2 — Instance Identification

| Field | Description |
|---|---|
| Instance Name | A human-readable name shown on other instances (e.g. "House", "Apartment 3") |
| Instance ID | Auto-generated UUID — used internally in MQTT topics. You can leave the default. |

### Step 3 — Select what to share

| Field | Description |
|---|---|
| Devices to share | Select full devices — all their entities will be shared |
| Additional entities to share | Select individual entities that don't belong to a device (e.g. template sensors, helpers) |

Click **Submit**. The integration will connect to the MQTT broker and start publishing.

### On the other instance(s)

Repeat the same setup steps on every other HA instance. Use the **same MQTT broker** but a **different Instance Name**. Each instance will automatically discover and create all shared devices and entities from other instances.

## Options

After setup, you can reconfigure the integration via **Settings → Integrations → Shared Home Assistant → Configure**:

- **Update device/entity selection** — add or remove shared devices and entities
- **Entity prefix** — set a prefix for received entities (e.g. `house` → entities appear as `sensor.house_temperature`)

## How it works

Each instance connects to the shared MQTT broker and:

1. **Publishes** selected devices and entity states to `shared_ha/{instance_id}/devices/...` and `shared_ha/{instance_id}/states/...` with `retain=true`
2. **Subscribes** to all other instances' device and state topics
3. **Creates local entities** in the HA device/entity registry that mirror the remote state
4. **Forwards service calls** (e.g. `switch.turn_on`) back to the origin instance via `shared_ha/{instance_id}/commands/...`
5. **Publishes a heartbeat** with MQTT Last Will so other instances detect when it goes offline

Messages use `retain=true`, so a newly connected instance immediately receives the current state of all shared entities without waiting for a state change.

## MQTT Topic Structure

```
shared_ha/{instance_id}/devices/{device_id}    # Device metadata (retained)
shared_ha/{instance_id}/states/{entity_id}     # Entity state updates (retained)
shared_ha/{instance_id}/commands/{entity_id}   # Service call forwarding (not retained)
shared_ha/{instance_id}/heartbeat              # Online/offline status (retained + LWT)
```

## Troubleshooting

**Entities show as "unavailable"**
The source instance is offline or disconnected from the MQTT broker. Check the source instance's logs and broker connectivity.

**Entities show as "Unknown"**
The entity was created but no state has been received yet. This can happen briefly after setup — wait a few seconds for retained messages to arrive, or trigger a state change on the source instance.

**Integration fails to load**
Check the HA logs for errors. Common causes:
- MQTT broker unreachable (wrong host/port)
- `aiomqtt` dependency not installed (restart HA after HACS install)

**Shared entities not appearing on the other instance**
- Verify both instances point to the same MQTT broker
- Check with an MQTT client (e.g. MQTT Explorer) that topics under `shared_ha/` contain data
- Ensure the receiving instance has been restarted after installing the integration

## License

MIT
