# Shared Home Assistant

Share devices, entities, and dashboards between multiple Home Assistant instances using a central MQTT broker.

## Features

- **Share devices and entities** from one HA instance to any number of other HA instances
- **Share dashboards** — display a dashboard from one instance in another instance's sidebar, including custom JS cards, SVGs, and animations — no entity sharing required
- **Read-write or read-only sharing** — choose per device/entity whether other instances can control it or only see its state
- **Bidirectional control** — shared read-write switches, lights, covers, and climate entities can be controlled from any instance via MQTT command forwarding
- **Real-time state sync** — state changes are published instantly via MQTT with retained messages
- **Automatic history transfer** — long-term statistics (hourly data) are transferred on first share and incrementally after downtime
- **Availability tracking** — heartbeat with MQTT Last Will detects offline instances
- **Independent MQTT connection** — uses `aiomqtt` separately from your existing HA MQTT integration

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

## Limitations

- **No sub-hourly history import** — only long-term statistics (hourly aggregates) are transferred. Detailed sub-minute history builds up locally after the entity appears.
- **No dashboard config sync** — dashboards are displayed live from the source instance via iframe. They are not copied — the source must be reachable from the user's browser.
- **No automatic HACS card installation** — custom frontend cards must be installed manually on each instance for entity sharing. Dashboard sharing does NOT have this limitation since cards are rendered by the source.
- **No conflict resolution** — if two instances share entities with the same entity_id, behavior is undefined.
- **Startup delay** — after a restart, it may take 30–60 seconds before all shared entities receive their first state update. This is normal — the integration needs to establish MQTT connections, receive retained messages, and create entities.

## Requirements

- Home Assistant **2026.3** or newer
- An MQTT broker accessible by all instances (e.g. [Mosquitto](https://mosquitto.org/))
- For dashboard sharing: both instances must be accessible via **HTTPS**

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
| Instance Name | Human-readable name shown on other instances (e.g. "House", "Apartment") |
| Instance ID | Auto-generated UUID. Leave the default. |

### Step 3 — Select what to share

| Field | Description |
|---|---|
| Devices (read-write) | Full devices with control from other instances |
| Devices (read-only) | Full devices, state only — no control |
| Entities (read-write) | Individual entities with control |
| Entities (read-only) | Individual entities, state only |

Click **Submit**. The integration connects to the MQTT broker and starts publishing.

### On the other instance(s)

Repeat the same steps on every HA instance. Use the **same MQTT broker** but a **different Instance Name**. Each instance automatically discovers and creates shared devices and entities from other instances.

## Options

Reconfigure via **Settings → Integrations → Shared Home Assistant → Configure**:

**Step 1 — Devices & Entities:**
- Update device/entity selection and read-write/read-only mode
- Set an entity prefix for received entities (e.g. `house` → `sensor.house_temperature`)

**Step 2 — Dashboard Sharing:**
- Enable/disable dashboard sharing
- Set this instance's HTTPS URL
- Select which dashboards to share

## Dashboard Sharing

Share entire dashboards (including custom JS cards, SVGs, and animations) from one instance to another without sharing individual entities.

### Prerequisites

Both instances must be accessible via **HTTPS** (e.g. behind a reverse proxy). Dashboard sharing uses a direct iframe — mixed HTTP/HTTPS content is blocked by browsers.

### Setup on the source instance

**1. Add to `configuration.yaml` and restart:**

```yaml
http:
  use_x_frame_options: false
```

This allows the dashboard to be embedded in an iframe on other instances.

**2. Configure in the integration options:**

1. **Settings → Integrations → Shared Home Assistant → Configure**
2. Step 1: click Submit (or update entity sharing)
3. Step 2:
   - Enable **"Enable dashboard sharing"**
   - Set **"This instance's URL"** — must be the **HTTPS** URL (e.g. `https://ha-house.example.com`)
   - Select which dashboards to share
   - Click Submit

### Setup on the receiving instance

No configuration needed. Shared dashboards appear in the sidebar automatically. The `?kiosk` parameter is appended to hide the source's header and sidebar (requires [kiosk-mode](https://github.com/NemesisRE/kiosk-mode) installed on the source).

### First use

When you first open a shared dashboard, you'll see the source instance's login page inside the iframe. **Log in once** — the browser remembers the session. After that, the dashboard loads automatically on every visit.

### Sidebar naming

- The **default dashboard** (Overview/Übersicht) appears in the sidebar with the **instance name** (e.g. "House")
- All **other dashboards** keep their original name (e.g. "Energy Flow")

### What works

- Custom Lovelace cards (including HACS cards)
- Custom JS dashboards with SVGs and animations
- Live data updates via WebSocket
- Kiosk mode (hides header and sidebar)
- All standard HA dashboard features

### Security

- No sensitive tokens are exposed to the browser
- The `use_x_frame_options: false` setting only allows iframe embedding — it does not disable authentication
- Users authenticate directly with the source instance via standard HA login

## How it works

### Real-time state sync

Each instance connects to the shared MQTT broker and:

1. **Publishes** selected devices and entity states with `retain=true`
2. **Subscribes** to all other instances' topics
3. **Creates local entities** that mirror the remote state
4. **Forwards service calls** back to the origin instance via MQTT (read-write entities only)
5. **Publishes a heartbeat** with MQTT Last Will for offline detection

Retained messages ensure new instances immediately receive all current states.

### History transfer

When a shared entity first appears on a receiving instance:

1. Subscriber requests statistics from the source via MQTT
2. Source queries its recorder database and sends chunked responses
3. Subscriber imports data using HA's `async_import_statistics` API
4. On reconnect after downtime, only the missing period is synced

**Transferred:** hourly aggregated statistics (mean, min, max, sum, state) — shown in long-term history graphs and energy dashboard.

**Not transferred:** sub-minute raw state changes and 5-minute short-term statistics — these build up locally from live state changes.

### Dashboard sharing

Dashboards are shared via direct iframe embedding:

1. Source publishes its HTTPS URL and dashboard list via MQTT
2. Receiving instances register sidebar panels pointing to the source
3. The HA frontend runs natively on the source — correct routing, WebSocket, auth, and custom cards

## MQTT Topic Structure

```
shared_ha/{instance_id}/devices/{device_id}                               # Device metadata (retained)
shared_ha/{instance_id}/states/{entity_id}                                # Entity state updates (retained)
shared_ha/{instance_id}/commands/{entity_id}                              # Service call forwarding (not retained)
shared_ha/{instance_id}/heartbeat                                         # Online/offline status (retained + LWT)
shared_ha/{instance_id}/history_request/{entity_id}                       # History request (not retained)
shared_ha/{instance_id}/history_response/{requester_id}/{entity_id}/{n}   # History chunks (not retained)
shared_ha/{instance_id}/history_response/{requester_id}/{entity_id}/done  # History complete (not retained)
shared_ha/{instance_id}/dashboards                                        # Dashboard discovery (retained)
```

## Troubleshooting

**Entities show as "unavailable"**
The source instance is offline or disconnected from the MQTT broker.

**Entities show as "Unknown" after restart**
Normal during startup. Wait 30–60 seconds for the integration to connect, receive retained messages, and sync states.

**History not appearing in graphs**
Check HA logs for "Imported X statistics rows". If the source entity has no long-term statistics (e.g. just created), there's nothing to transfer.

**Shared dashboard shows login page**
Expected on first use per browser. Log in once — the session persists. If it keeps showing login, verify `use_x_frame_options: false` on the source and that the HTTPS URL is correct.

**Shared dashboard shows "refused to connect"**
The source instance's reverse proxy may be blocking iframe embedding. Add to your reverse proxy config:
```nginx
proxy_hide_header X-Frame-Options;
proxy_hide_header Content-Security-Policy;
```

**Shared dashboard not appearing in sidebar**
- Verify dashboard sharing is enabled on the source instance
- Check the HTTPS URL is set correctly in options
- Restart the receiving instance to pick up MQTT changes

**Read-only entity shows controls**
The HA frontend may show toggle/slider controls, but using them has no effect — commands are blocked.

**Entities not updating after restart**
The integration takes 30–60 seconds after HA startup to fully initialize. MQTT connections may be unstable during the startup phase.

## License

MIT
