# CONTEXT.md — TransitGuard orientation & handover

Written for any coding agent or developer picking this project up cold.
Everything here was verified against the live system on **2026-10-16**.

---

## 1. What this project is

TransitGuard is a driver-fatigue and vehicle-monitoring system split across **two repos**:

```
 Raspberry Pi 4 (vehicle)                    Supabase                          Browser
 ┌──────────────────────────┐   HTTPS    ┌───────────────────────┐        ┌──────────────────┐
 │ vehicle_monitor.py       │──────────► │ Edge Function          │        │ Dashboard (Vite) │
 │  • MediaPipe face/eye    │  POST /    │  ingest-telemetry      │        │ transit-guard-   │
 │  • fatigue classification│  functions │        │               │        │ io-t.vercel.app  │
 │  • MQ-3 alcohol (ADS1115)│  /v1/...   │        ▼               │        └────────▲─────────┘
 │  • GPS (NEO-6M / UART0)  │            │  Postgres              │                 │
 │  • LoRa (ATK-LORA-01)    │            │  public.telemetry      │─────────────────┘
 │  • offline SQLite buffer │            │  (RLS: SELECT+INSERT)  │   Supabase JS (REST + Realtime)
 │  • Flask dashboard :8080 │            └───────────────────────┘
 └──────────────────────────┘
```

The Pi is the data source; Supabase is the store and the ingest path; the Vercel dashboard is the
consumer. **The upload path (Pi → Edge Function → Postgres → dashboard) is the subject of the
connection-stability measurement documented in `docs/measurements/stability_report.md`, and its
failure behaviour in `docs/measurements/connection_loss_report.md`.**

---

## 2. Repositories and local locations

| Repo | GitHub | Local path (this machine) | Role |
|---|---|---|---|
| **TransitGuard_Pi** | https://github.com/LORDTOSIN2/TransitGuard_Pi.git | `E:\HOME\CODES\2026\transitguard-repo` | Pi application (`vehicle_monitor.py`), diagnostic tools, README |
| **TransitGuard-IoT** | https://github.com/LORDTOSIN2/TransitGuard-IoT.git | `E:\HOME\CODES\2026\TransitGuard-IoT` | Supabase schema + `ingest-telemetry` Edge Function + Vite/React dashboard |

Both are **public**. `TransitGuard_Pi` is the repo that is actually cloned onto the Pi.

**On the Pi**, the same repo is checked out at `/home/john/fatigue` (same `origin`).
As of writing, the **code** state is `TransitGuard_Pi` @ `668445a` (the checkout on the Pi)
and `TransitGuard-IoT` @ `91558e0`; later commits in both repos are documentation only. The IoT
dashboard now tags every vehicle `LIVE` vs `SIM`, prefers real telemetry over the demo
fleet, gates the simulator behind a toggle and ships a network-first service worker — see
the IoT repo's [`CONTEXT.md`](https://github.com/LORDTOSIN2/TransitGuard-IoT/blob/main/CONTEXT.md).

> **Committed measurement records** live in this repo under [`docs/measurements/`](docs/measurements/)
> (the three-run connection-stability study plus the raw traces, CSVs and charts, **and the
> connection-loss & recovery study** in `connection_loss_report.md`) and
> [`docs/dashboard-test/`](docs/dashboard-test/) (the local + remote dashboard test, including
> redacted screenshots and the DB evidence). The **unredacted** campaign files stay in
> `C:\Users\Administrator\Documents\Default Project`.

---

## 3. The Pi (production device)

| Item | Value |
|---|---|
| Host | `john` @ **`10.80.3.194`** (address is on the SFM hotspot subnet — see §5) |
| OS / arch | Debian GNU/Linux 12 (bookworm), `aarch64` (Raspberry Pi 4) |
| App directory | `/home/john/fatigue` (git clone of `TransitGuard_Pi`) |
| Entry point | `/home/john/fatigue/vehicle_monitor8.py` → **symlink to `vehicle_monitor.py`** |
| Python | `/home/john/fatigue/fatigue-env/bin/python3` (Python 3.11.2) |
| Service | `transitguard.service` — `/etc/systemd/system/transitguard.service` |
| Service config | `User=john`, `WorkingDirectory=/home/john/fatigue`, `EnvironmentFile=/etc/transitguard.env`, `Restart=on-failure`, `RestartSec=5` |
| Env file | `/etc/transitguard.env` — `root:john`, mode `640` |
| Dashboard | `http://10.80.3.194:8080/` (Flask, bound `0.0.0.0:8080`) |
| Camera | USB UVC webcam → `/dev/video0` |
| GPS | NEO-6M → `/dev/serial0` → `/dev/ttyS0`, 9600 baud |
| LoRa | ATK-LORA-01 → `/dev/ttyAMA2` (enabled via `dtoverlay=uart2`) |
| Logs | `/home/john/fatigue/logs/` (`fatigue_log_*.csv`, `telemetry_trace*.jsonl`) and `/home/john/fatigue/vehicle_monitor_log.csv` |

> **Gotcha:** the service runs `vehicle_monitor8.py`, which is a *symlink* to `vehicle_monitor.py`.
> Edit `vehicle_monitor.py`; there is only one real file.

### Dashboard endpoints

| Route | Purpose |
|---|---|
| `GET /` | HTML dashboard (live video + metrics) |
| `GET /video_feed` | MJPEG stream |
| `GET /data` | JSON metrics: `driver_state`, `ear/mar/perclos/pitch`, `latitude/longitude/fix_valid`, `sats_used/sats_in_view/best_snr/fix_quality`, `buffer_count`, `alert_coalesced`, `pi_temperature`, `pi_temp_level`, `pi_throttled`, `updated_at` |
| `GET /wifi/status` | Current SSID + which network the switch button would target |
| `POST /wifi/switch` | Performs the Wi-Fi switch (see §5) |
| `GET /temperature` (alias `/temp`) | Dedicated Pi SoC temperature page — big colour-coded readout, throttling note, HOT banner |
| `GET /temperature/data` | JSON feed for that page: `temperature`, `level`, `throttled`, `note`, `updated_at` |

`/data.updated_at` is the **main-loop heartbeat**. If it is `null`, the loop is not running
(usually a camera problem) and no telemetry is being produced at all.

**Pi temperature.** `GET /temperature` (alias `/temp`) is a dedicated at-a-glance page for
SoC heat, and the main dashboard shows the same value in the top bar, the Live Metrics card
and the status strip. Levels: **cool** < 70 °C, **warm** 70–80 °C, **hot** 80–85 °C,
**critical** ≥ 85 °C (the Pi's soft limit). The reading comes from
`/sys/class/thermal/thermal_zone0/temp`; the throttling flags come from
`vcgencmd get_throttled`. Colour is never the only signal — every indicator also carries
the value as text and in an `aria-label`.

### Deploying an update to the Pi

```bash
ssh john@10.80.3.194
cd /home/john/fatigue
GIT_TERMINAL_PROMPT=0 git pull --ff-only origin main
./fatigue-env/bin/python3 -m py_compile vehicle_monitor.py     # fail fast on syntax errors
sudo systemctl restart transitguard
sleep 50
curl -s http://127.0.0.1:8080/data | head -c 300               # confirm updated_at is non-null
```

---

## 4. SSH access

```bash
ssh john@10.80.3.194
```

* **Key-based auth** — `~/.ssh/authorized_keys` exists on the Pi (no password prompt).
* `john` has **passwordless sudo** (`NOPASSWD: ALL`, via `/etc/sudoers.d/010_pi-nopasswd`).
* `scp` works the same way, e.g.
  `scp john@10.80.3.194:/home/john/fatigue/logs/telemetry_trace_run3.jsonl .`

**Windows/PowerShell quirk:** raw `<`, `$(...)`, and unbalanced quotes get mangled by the
PowerShell parser when passed to `ssh`. Base64-encode remote scripts instead:

```powershell
$script = @'
echo "hello from the pi"; date -u
'@
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($script))
ssh john@10.80.3.194 "echo $b64 | base64 -d | bash"
```

**If the address changes:** `10.80.3.194` belongs to the SFM hotspot subnet. Switch networks
(§5) and the address changes; also check the LCD, which shows the IP on boot.

---

## 5. Wi-Fi / hotspot switching (Redmi 14C ⇄ SFM MBB - CONF)

Switching is done with **NetworkManager profiles**, not `iwgetid`. The Pi always boots onto
whichever profile has `autoconnect=yes`.

| NM profile name | SSID | `autoconnect` | Role |
|---|---|---|---|
| **`preconfigured`** | **`Redmi 14C`** | **yes** | **Default — a power cycle always returns here** |
| **`SFM MBB - CONF`** | `SFM MBB - CONF` | **no** | Secondary — must be selected manually |
| `Wired connection 1` | — | yes | Ethernet |

> **The profile name and the SSID differ.** The Redmi 14C network is stored under the profile
> name `preconfigured`; there is no profile literally called `Redmi 14C`. The code comments in
> `vehicle_monitor.py` call this out explicitly.

**Mechanism** (`vehicle_monitor.py`): `get_wifi_info()` reads the SSID (via `nmcli`) and the
signal level (via `/proc/net/wireless`); `_nmcli()` wraps `nmcli` with `sudo -n`;
`_ensure_profile()` creates the secondary profile with `connection.autoconnect no`;
`schedule_wifi_switch(profile, delay)` runs `sleep <delay>; nmcli connection up <profile>`
in the background (deferred so the HTTP response can be flushed first).

**From the dashboard:** the Wi-Fi button calls `POST /wifi/switch`, which targets
`WIFI_SWITCH_TARGET_SSID` when on the home network, or `WIFI_SWITCH_HOME_SSID` when already on
the secondary one (`GET /wifi/status` reports which).

**Config keys** (all in `/etc/transitguard.env`):

| Key | Meaning |
|---|---|
| `WIFI_SWITCH_ENABLED` | feature flag |
| `WIFI_SWITCH_HOME_SSID` | `Redmi 14C` (the autoconnect default) |
| `WIFI_SWITCH_TARGET_SSID` | `SFM MBB - CONF` (the manual secondary) |
| `WIFI_SWITCH_TARGET_PASSWORD` | **SECRET — env file only, never commit** |

**Consequence:** the SFM profile is deliberately `autoconnect=no`, so a power cycle always
returns the Pi to Redmi 14C. If you switched to SFM and the Pi loses power, expect it back on
Redmi 14C (and at a different IP).

---

## 6. Supabase

| Item | Value |
|---|---|
| Project ref | `hfzqfcuezuxcgaifxqok` |
| API / project URL | `https://hfzqfcuezuxcgaifxqok.supabase.co` |
| Ingest endpoint | `https://hfzqfcuezuxcgaifxqok.supabase.co/functions/v1/ingest-telemetry` |
| Edge Function source | `TransitGuard-IoT/supabase/functions/ingest-telemetry/index.ts` (Deno) |
| Table | `public.telemetry` |
| Dashboard (frontend) | **https://transit-guard-io-t.vercel.app/** |
| Publishable (anon) key | `sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF` — **already public** in `TransitGuard-IoT/src/services/supabaseService.ts` and `rpi.env.example` |

**`public.telemetry` essentials**
* `message_id TEXT NOT NULL UNIQUE` — the dedupe key.
* `created_at TIMESTAMPTZ DEFAULT now()` — the **server receive time** (used as the authority
  for storage and for end-to-end delay).
* RLS: **public SELECT + INSERT only** — no UPDATE/DELETE. Read access needs no extra
  credentials.
* Realtime is enabled (`ALTER PUBLICATION supabase_realtime ADD TABLE public.telemetry`).

**Critical ingest behaviour**
* The Edge Function upserts with `onConflict: 'message_id', ignoreDuplicates: true`.
* Therefore a **duplicate returns HTTP 201 but inserts nothing** — you cannot detect duplicates
  from the HTTP status. Duplicate/repeat counts must be derived **server-side**.
* Column mapping includes `satellite_count: payload.location?.satelliteCount ?? 0`, and the
  entire posted JSON is preserved in `raw_payload` (so extra location fields are safe).

**Device auth:** the Pi authenticates with `TRANSITGUARD_DEVICE_TOKEN` (§7) — a **secret**,
present both in `/etc/transitguard.env` and in the Supabase Edge Function secrets.

**Payload shape** (abridged):

```jsonc
{
  "deviceId": "BUS-101",
  "messageId": "rpi-<uuid>",
  "timestamp": 1792131707222,          // device ms since epoch
  "driverState": "NORMAL",             // NORMAL | DROWSY | CRITICAL_FATIGUE
  "location": { "lat": null, "lng": null, "hasFix": false, "satelliteCount": 0,
                "satellitesInView": 0, "bestSnr": 29, "fixQuality": 0 },
  "transport": { "wifiSsid": "...", "wifiRssi": -66, ... },
  "driverMetrics": { ... }, "alcohol": { ... }
}
```

---

## 7. `/etc/transitguard.env` — key names

Values are **withheld on purpose**. Current key set (verified 2026-10-16):

```
BUFFER_DB_PATH              ENABLE_PREVIEW            MAX_BUFFERED_PACKETS
PUBLISH_INTERVAL_SEC        SUPABASE_INGEST_URL       TRANSITGUARD_DEVICE_ID
TRANSITGUARD_DEVICE_TOKEN   WIFI_SWITCH_ENABLED       WIFI_SWITCH_HOME_SSID
WIFI_SWITCH_TARGET_PASSWORD WIFI_SWITCH_TARGET_SSID
# evaluation-only, ABSENT unless a measurement run is in progress:
ENABLE_TEST_STATE_INPUT     TEST_STATE_INPUT_PATH
```

**Secrets in this file — never commit:** `TRANSITGUARD_DEVICE_TOKEN`,
`WIFI_SWITCH_TARGET_PASSWORD`. (Any `SUPABASE_SERVICE_ROLE_KEY` lives only in Supabase secrets.)

Keys that are **absent** and therefore use code defaults: `ALERT_PUBLISH_MIN_INTERVAL_SEC` (5.0),
`TELEMETRY_QUEUE_MAXSIZE` (50), `TELEMETRY_TRACE_PATH` (unset ⇒ **tracing off**).

Backups exist alongside it: `/etc/transitguard.env.bak-run2-*`, `.bak-run3-*`, `.bak-trace-*`.

---

## 8. Telemetry behaviour worth knowing before changing the upload path

* `PUBLISH_INTERVAL_SEC = 10` — normal cadence is one message / 10 s.
* `telemetry_queue` — bounded, **drop-oldest**, `maxsize = TELEMETRY_QUEUE_MAXSIZE` (50).
* **Alert rate limit** — while an alert is active (or a transition just occurred) the minimum gap
  between sends is `ALERT_PUBLISH_MIN_INTERVAL_SEC` (5 s). *Before commit `46077a2` the loop
  enqueued a message per frame (~20 FPS), which overflowed the queue and silently dropped ~49 % of
  alert messages — see `docs/measurements/stability_report.md`.*
* `HTTP_TIMEOUT_SEC = 6` is applied **per socket operation**, not per request, which is why
  multi-second requests can still succeed.
* Offline **SQLite buffer** handles persistence/retry when the primary path is down
  (`BUFFER_DB_PATH`, `MAX_BUFFERED_PACKETS`).
* **`print()` uses block-buffered stdout under systemd**, so the journal under-reports
  `[SENT via WiFi/Cellular]`. To verify liveness, check Supabase `created_at` (or `/data`),
  not the journal.
* `lora_queue` is still `maxsize=5` with drop-oldest — the residual exposure noted in §10.

---

## 9. Connection-stability measurement tooling

* Tracing is **gated and off by default**. Enable by adding to `/etc/transitguard.env`:
  `TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace_runN.jsonl`, then restart.
  It emits JSONL `scheduled` / `attempt` / `dropped` events with `t_mono_start`,
  `t_mono_end`, `rtt_ms`, `http_status`, `server_date`, `error`.
  **Turn it off afterwards** — it grows ~5.8 MB/day and the disk is ~91 % full.
* Tools in `TransitGuard_Pi/tools/`: `analyze_session.py` (trace ↔ Supabase reconciler),
  `chart_session.py` (matplotlib charts), `export_messages.py` (per-message CSV).
* **Connection-loss & recovery.** `tools/run_outage_trials.sh` black-holes **only** the Supabase
  ingest route (Cloudflare ranges + resolved /32s) for 3 × 5 min, asserting the cut/restore and
  using `tools/analyze_outage.py` to reconcile the trace against Supabase. Results (2026-10-16):
  **3 / 3 recovered, 0 dropped, 93 / 93 outage-scheduled messages stored**, buffer → 0 in
  89–123 s — see [`docs/measurements/connection_loss_report.md`](docs/measurements/connection_loss_report.md).
  It pairs with a temporary trace drop-in
  (`/etc/systemd/system/transitguard.service.d/zz-outage-test.conf`) that **must be removed
  afterwards**.
* `transitguard-repo/README.md` has a "Measuring connection stability" section describing the
  trace format, duplicate handling and clock caveats.
* **Post-mitigation sessions.** `tools/run_session.py` runs one measured session (safety
  preflight ≤ 86 °C + cool-down gate, unique `logs/runs/<UTC>_s<N>/` folder, manifest, 10 s
  sampling, 3-min scripted flap window, guaranteed teardown, pre/post `get_throttled`).
  `tools/analyze_campaign.py` reconciles the traces against Supabase and emits per-session and
  combined counts, queue high-water, cadence and RTT percentiles. The scripted state input
  (`ENABLE_TEST_STATE_INPUT` + `TEST_STATE_INPUT_PATH`) is test-only, off by default, and marks
  every affected packet with `testInjection`; see `docs/measurements/post_mitigation/`.

**Headline measured results (3 × ~30 min runs, 2026-10-16, SFM MBB - CONF):**

| Metric | Value |
|---|---|
| Scheduled / attempted | 612 / 580 |
| Accepted & stored unique | 578 |
| Dropped from queue | 34 (all in one alert burst, run 3) |
| Failed HTTP | 2 (`ReadTimeout`, both retried from buffer ⇒ no loss) |
| Delivery vs attempted | **99.66 %** |
| Delivery vs scheduled | **94.44 %** |
| HTTP RTT | median **1.65 s**, p95 **3.79 s**, max 17.0 s (578 samples) |
| Alert-path loss before the fix | **49 %** (fixed in `46077a2`) |

---

## 10. Current known state & gotchas

1. **GPS has never achieved a fix.** The module is alive and streams clean NMEA, but reports
   `fix_quality 0`, `sats_used 0`, `sats_in_view 0–2`, `best C/N0 29 dBHz` (a fix needs ≥4 sats
   at ≈35–40 dBHz). This is an **antenna / sky-view** limitation, not a Pi or serial problem.
   Since commit `c53d1e3` the app asks the module for GGA/GSA/GSV via `UBX-CFG-MSG`
   (`GPS_ENABLE_SENTENCES`), so these diagnostics are available. There is no
   `.gps_last_known.json` yet — expect it to appear when a fix is first achieved.
2. **USB camera fragility.** After some restarts the UVC camera fails to stream
   (`uvcvideo: Failed to set UVC probe control`), `cv2.VideoCapture` still reports *opened*, and
   `read()` always fails. Commit `c53d1e3` replaced the old tight retry loop (which pegged a core
   at ~98 % CPU and silently stopped all telemetry) with backoff + periodic reopen. If `/data`'s
   `updated_at` stays `null` after a restart, **restart the service again**.
3. **Disk is ~91 % full** (~586 MB free). This is why tracing is kept off.
4. **`lora_queue` is still `maxsize=5`** with drop-oldest — the same overflow exposure the
   telemetry queue had, should LoRa forwarding ever be exercised during a long alarm.
5. The Pi's journal timestamps are local (`WAT`, UTC+1); Supabase timestamps are UTC.
6. `/home/john/obsolete-prototypes-20261016_001657.tar.gz` is a safety archive of removed
   prototypes — do not delete without checking.
7. **Live power warning (2026-10-21):** `get_throttled` reads `0x50005` — under-voltage is
   present *now*, throttling has occurred, and the thermal soft limit has engaged. SoC idles
   around 74 °C with the camera running. Treat long measurement runs accordingly, record the
   readings, and do not describe a run as clean when this bitmask is non-zero.

---

## 11. Commit landmarks (`TransitGuard_Pi`)

| Commit | What |
|---|---|
| `8184e19` | fix: `conname` → `con-name` (nmcli) |
| `1ac6bc8` | fix: `get_wifi_info` via `nmcli` |
| `064f419` | feat: gated telemetry tracing (`TELEMETRY_TRACE_PATH`) |
| `9efe359` | feat: analysis/export tooling + README section |
| `c53d1e3` | fix: request & parse GPS GGA/GSV; stop the camera busy-spin |
| `46077a2` | fix: rate-limit the alert publish path; raise queue maxsize |

---

## 12. Quick reference

```bash
# Health check
ssh john@10.80.3.194 'systemctl is-active transitguard; \
  curl -s http://127.0.0.1:8080/data | head -c 300'

# Restart (also the fix for a stalled camera)
ssh john@10.80.3.194 'sudo systemctl restart transitguard'

# Which network am I on?
ssh john@10.80.3.194 'nmcli -t -f ACTIVE,SSID,SIGNAL dev wifi | grep "^yes"'

# Watch the app log
ssh john@10.80.3.194 'sudo journalctl -u transitguard -n 40 --no-pager'

# Read telemetry straight from Supabase
curl -s "https://hfzqfcuezuxcgaifxqok.supabase.co/rest/v1/telemetry?select=created_at,driver_state,wifi_rssi,satellite_count&device_id=eq.BUS-101&order=created_at.desc&limit=5" \
  -H "apikey: sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF"
```

## 13. Never commit

* `TRANSITGUARD_DEVICE_TOKEN`, `WIFI_SWITCH_TARGET_PASSWORD` (in `/etc/transitguard.env`)
* Any Supabase **service role** key
* `.env` / `rpi.env` files with real values — `rpi.env.example` is the template
* Real Wi-Fi passwords of any kind

Both repos are **public**.
