# Shared Home Assistant

Share devices and entities between multiple Home Assistant instances using a central MQTT broker.

## What it does

- **Share devices and entities** from one HA instance to any number of other HA instances
- **Read-write or read-only sharing**: choose per device/entity whether other instances can control it or only see its state
- **Bidirectional control**: shared read-write switches, lights, covers, and climate entities can be controlled from any instance — commands are forwarded back to the origin instance via MQTT
- **Real-time state sync**: state changes are published instantly via MQTT with retained messages, so new instances receive the current state immediately on connect
- **Automatic history transfer**: when an entity is first shared, its long-term statistics (hourly data) are automatically transferred to all receiving instances. After downtime, only the missing period is synced.
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

- **No sub-hourly history import** — history sync transfers long-term statistics (hourly aggregates). The detailed sub-minute state history from the source instance's last ~10 days is not transferred. New detailed history builds up locally from the moment the shared entity appears.
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
| Devices (read-write) | Full devices with control — other instances can send commands (turn on/off, etc.) |
| Devices (read-only) | Full devices, state only — other instances can see the state but not control |
| Entities (read-write) | Individual entities with control |
| Entities (read-only) | Individual entities, state only |

Click **Submit**. The integration will connect to the MQTT broker and start publishing.

### On the other instance(s)

Repeat the same setup steps on every other HA instance. Use the **same MQTT broker** but a **different Instance Name**. Each instance will automatically discover and create all shared devices and entities from other instances.

## Options

After setup, you can reconfigure the integration via **Settings → Integrations → Shared Home Assistant → Configure**:

- **Update device/entity selection** — add or remove shared devices and entities, change between read-write and read-only
- **Entity prefix** — set a prefix for received entities (e.g. `house` → entities appear as `sensor.house_temperature`)

## How it works

### Real-time state sync

Each instance connects to the shared MQTT broker and:

1. **Publishes** selected devices and entity states to `shared_ha/{instance_id}/devices/...` and `shared_ha/{instance_id}/states/...` with `retain=true`
2. **Subscribes** to all other instances' device and state topics
3. **Creates local entities** in the HA device/entity registry that mirror the remote state
4. **Forwards service calls** (e.g. `switch.turn_on`) back to the origin instance via `shared_ha/{instance_id}/commands/...` (only for read-write entities)
5. **Publishes a heartbeat** with MQTT Last Will so other instances detect when it goes offline

Messages use `retain=true`, so a newly connected instance immediately receives the current state of all shared entities without waiting for a state change.

### History transfer

When a shared entity is first created on a receiving instance, it automatically requests the source instance's long-term statistics (hourly aggregates) via MQTT:

1. **Subscriber** sends a history request to the source instance specifying "since when" it needs data
2. **Source** queries its own HA recorder database for statistics
3. **Source** sends the data back in chunked MQTT messages (100 rows per chunk)
4. **Subscriber** imports the data into its local recorder using HA's `async_import_statistics` API

On subsequent reconnects (e.g. after an instance was offline for a day), only the missing time period is requested — the subscriber tracks the timestamp of its last imported statistic per entity.

**What gets transferred:**
- Hourly aggregated statistics (mean, min, max, sum, state) — the same data shown in HA's long-term history graphs and energy dashboard
- This data is **never automatically purged** by HA, so you get the full history going back to when the entity was first created

**What does NOT get transferred:**
- Sub-minute raw state changes (the detailed graph data for the last ~10 days) — this builds up locally from the moment the shared entity appears
- Short-term 5-minute statistics — these are auto-generated by HA from live state changes

## MQTT Topic Structure

```
shared_ha/{instance_id}/devices/{device_id}                             # Device metadata (retained)
shared_ha/{instance_id}/states/{entity_id}                              # Entity state updates (retained)
shared_ha/{instance_id}/commands/{entity_id}                            # Service call forwarding (not retained)
shared_ha/{instance_id}/heartbeat                                       # Online/offline status (retained + LWT)
shared_ha/{instance_id}/history_request/{entity_id}                     # History data request (not retained)
shared_ha/{instance_id}/history_response/{requester_id}/{entity_id}/{n} # History data chunks (not retained)
shared_ha/{instance_id}/history_response/{requester_id}/{entity_id}/done # History transfer complete (not retained)
```

## Troubleshooting

**Entities show as "unavailable"**
The source instance is offline or disconnected from the MQTT broker. Check the source instance's logs and broker connectivity.

**Entities show as "Unknown"**
The entity was created but no state has been received yet. This can happen briefly after setup — wait a few seconds for retained messages to arrive, or trigger a state change on the source instance.

**History not appearing in graphs**
History transfer imports hourly long-term statistics. It may take a moment after setup for the data to appear in history graphs. Check the HA logs for "Imported X statistics rows" messages. If the source entity has no long-term statistics yet (e.g. it was just created), there's nothing to transfer.

**Read-only entity still shows controls**
The HA frontend may show toggle/slider controls for entity types like switches and lights even when they're shared as read-only. Attempting to use the control will have no effect — the command is blocked on the receiving instance.

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
