# TransitGuard — Raspberry Pi 4 Driver Fatigue & Vehicle Monitoring System

A low-power IoT vehicle monitoring system built on a Raspberry Pi 4, combining
real-time driver fatigue detection, GPS tracking, alcohol sensing, and
dual-path telemetry (Wi-Fi/cellular primary, LoRa fallback) with local
audible/visual alerts and a live browser dashboard.

> **New here, or handing this over?** Read **[CONTEXT.md](CONTEXT.md)** first. It documents
> the two repositories, the Pi's layout and SSH access, the Wi-Fi/hotspot switching,
> Supabase, the telemetry/queue behaviour, the measurement tooling, and current gotchas.

## Features

- **Driver fatigue detection** — MediaPipe Face Landmarker (Tasks API), computing
  EAR, MAR, PERCLOS, and head pose (pitch/yaw/roll) in real time
- **Pose-gated EAR/MAR** — eye/mouth metrics are only trusted when the face is
  reasonably frontal, preventing false alerts from head-angle distortion
- **Alcohol sensing** — MQ-3 gas sensor via an ADS1115 ADC, with a calibration
  workflow for Rs/Ro baseline
- **GPS tracking** — NEO-6M module (NMEA via UART), with automatic fallback to
  the last known good fix (with staleness tracking) when signal is lost
- **Dual-path telemetry** — primary upload to a Supabase Edge Function over
  Wi-Fi/cellular, with automatic LoRa fallback when the primary path is down
  (plus safety-critical alerts always relayed over LoRa for redundancy)
- **Offline resilience** — SQLite store-and-forward buffer for telemetry sent
  while offline, flushed automatically once connectivity returns
- **Local driver-facing alerts** — 16x2 LCD (4-bit mode) + buzzer
- **Live browser dashboard** — video feed + real-time metrics, served over the
  LAN, no monitor needed on the Pi itself
- **Manual Wi-Fi switching** — optional dashboard button to hop between the
  normal network and a secondary saved network, reverting to the normal one on
  reboot (see below)
- **Performance-tuned** — threaded camera capture and non-blocking
  network/radio/actuator I/O so slow Wi-Fi or LoRa never stalls the camera loop

## Hardware

| Component | Interface |
|---|---|
| Raspberry Pi 4 | — |
| USB/Pi camera | USB or CSI |
| NEO-6M GPS module | UART0 (`/dev/serial0`) |
| MQ-3 alcohol sensor | Analog, via ADS1115 ADC (I2C) |
| ATK-LORA-01 LoRa module | UART2 (`/dev/ttyAMA2`), GPIO0/GPIO1 |
| 16x2 LCD (HD44780) | 4-bit mode: RS, E, D4–D7 |
| Buzzer | GPIO18 |

### Wiring

```
NEO-6M GPS   -> Pi UART0 (/dev/serial0): VCC, GND, TX->GPIO15, RX->GPIO14
MQ-3         -> ADS1115 A0 -> Pi I2C (GPIO2=SDA, GPIO3=SCL)
ATK-LORA-01  -> Pi UART2 (/dev/ttyAMA2): TXD->GPIO1, RXD->GPIO0
               M0/M1 -> GPIO23/GPIO24, held LOW (normal transmission mode)
               Note: GPIO0/1 double as the HAT ID EEPROM pins — fine for a
               breadboard build, avoid this UART if adding a HAT with an ID EEPROM
16x2 LCD     -> RS=GPIO25, E=GPIO8, D4=GPIO7, D5=GPIO12, D6=GPIO16, D7=GPIO20
               RW->GND, contrast via a 10k pot on V0
Buzzer       -> GPIO18, GND
```

Enable the extra UART for LoRa in `/boot/firmware/config.txt`:
```
dtoverlay=uart2
```

## Setup

```bash
git clone <this-repo-url>
cd transitguard

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Download the MediaPipe face landmark model
wget -O face_landmarker.task https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task

# Configure your environment
cp .env.example .env
nano .env   # fill in your real Supabase URL/token, etc.
```

**MediaPipe version note:** newer MediaPipe releases removed the legacy
`mp.solutions` API in favor of the Tasks API used here. If you hit
`AttributeError: module 'mediapipe' has no attribute 'solutions'`, you're on
an incompatible version — this project targets the Tasks API
(`mediapipe.tasks.python.vision.FaceLandmarker`), which should work with
current releases out of the box.

## Calibration

EAR/MAR thresholds vary by individual eye/mouth shape. Before relying on
alerts, calibrate your own baseline:

```bash
python3 tools/calibrate_ear.py
```

Face the camera normally for 10 seconds; it prints a suggested
`EAR_THRESHOLD` to set in `vehicle_monitor.py`.

For the MQ-3 sensor, run a clean-air calibration first (see
`tools/mq3_reader.py --help`-style usage in the file header) to determine
your sensor's `R0` baseline before trusting alcohol readings.

## Running

```bash
# Interactive, with live preview window
python3 vehicle_monitor.py

# Diagnostic: send one test packet to confirm auth/connectivity
python3 vehicle_monitor.py --once

# Check the offline buffer
python3 vehicle_monitor.py --status
python3 vehicle_monitor.py --flush
```

Once running, the live dashboard (video feed + metrics) is available at:
```
http://<pi-ip-address>:8080
```

**Pi temperature:** open `http://<pi-ip-address>:8080/temperature` (alias `/temp`)
for a large, colour-coded SoC temperature readout with a HOT banner at ≥ 80 °C. The
same value also appears on the main dashboard — top bar, Live Metrics card and status
strip. Levels: **cool** < 70 °C, **warm** 70–80 °C, **hot** 80–85 °C, **critical** ≥ 85 °C
(the Pi's soft limit).

## Switching Wi-Fi from the dashboard (optional)

If the Pi normally sits on one hotspot but occasionally needs another (e.g. a
lab/campus AP), the dashboard can expose a **Network** card with a button that
switches between the two — without giving up the safe default:

- On boot the Pi always joins the profile that has `autoconnect=yes` (the
  normal network). Nothing changes that.
- The button activates a *second* saved profile on demand. That profile is
  stored with `autoconnect=no`, so **a power cycle always returns the Pi to the
  normal network** — switching is session-only by design.
- The button is bi-directional: on the normal network it offers the secondary
  one, and vice-versa.
- If the secondary AP disappears while in use, NetworkManager falls back to the
  normal network on its own.

Configure it in the env file (never commit the password to the repo — the repo
is public):

```
WIFI_SWITCH_ENABLED=true
WIFI_SWITCH_TARGET_SSID=My Other Network
WIFI_SWITCH_TARGET_PASSWORD=the-password
WIFI_SWITCH_HOME_SSID=My Normal Network
```

Leave `WIFI_SWITCH_TARGET_SSID` blank to hide the card entirely.

Notes:

- Switching networks severs the browser's connection to the dashboard, because
  the Pi changes subnet. Reconnect your device to the target network, then
  reach the Pi again by its mDNS name (`http://john.local:8080`) or its new IP
  (the Network card shows the current IP while connected).
- The switch runs a couple of seconds *after* the button responds, in a
  detached `systemd-run` unit, so it completes even though the HTTP reply is
  cut off mid-switch.
- The endpoint is unauthenticated, like the rest of the dashboard — anyone on
  the LAN can press it. Keep the dashboard on a trusted network.
- Requires `nmcli` (NetworkManager) and passwordless `sudo` for the service
  user.

## Auto-run on boot (systemd)

```bash
sudo cp systemd/transitguard.service /etc/systemd/system/
sudo nano /etc/systemd/system/transitguard.service   # adjust paths/user
sudo cp .env /etc/transitguard.env
sudo chown root:pi /etc/transitguard.env
sudo chmod 640 /etc/transitguard.env

sudo systemctl daemon-reload
sudo systemctl enable transitguard.service
sudo systemctl start transitguard.service
sudo systemctl status transitguard.service
```

Set `ENABLE_PREVIEW=false` in your env file for headless auto-run — there's
no display available under systemd, and the live dashboard is the intended
way to view the feed remotely instead.

## Standalone diagnostic tools (`tools/`)

- `calibrate_ear.py` — measures your personal EAR baseline
- `gps_reader.py` — standalone NEO-6M NMEA reader/parser
- `mq3_reader.py` — standalone MQ-3 calibration and live reading
- `lora_at_config.py` — AT-command configuration/testing for the ATK-LORA-01
- `analyze_session.py` — reconciles a telemetry trace against Supabase (delivery
  ratio, scheduled/attempted/accepted/failed/duplicate counts, RTT vs. end-to-end)
- `chart_session.py` — renders the latency/stability charts (requires `matplotlib`)
- `export_messages.py` — one-row-per-message CSV export, reconciled with the DB
- `measure_fps.py` — client-side frame rate of `/video_feed` (frames/s, JPEG dimensions, inter-frame gaps)
- `wifi_latency_test.py` — ICMP / TCP / HTTP latency and PDR sweeps, idle vs. under video load; writes `wifi_latency_results.csv` to the working directory (lab hosts are constants at the top of the file)

## Measuring connection stability

The app can optionally record a per-event JSONL trace of its upload path, used
to evaluate connection stability and delivery ratio:

```bash
# /etc/transitguard.env (or .env) — tracing is off when unset/empty
TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace.jsonl
```

While set, each publish appends one JSON line per event:

- `scheduled` — the main loop queued a payload (`trigger`: `interval` or `alert`)
- `attempt` — an HTTP POST finished; carries `t_mono_start` / `t_mono_end`
  (from `time.monotonic()`), `rtt_ms`, `http_status`, `error`, and the server's
  `Date` header
- `dropped` — the queue was full and evicted its oldest pending payload

Tracing is fully disabled when the path is empty, so behaviour is unchanged in
normal operation. After a measurement run, reconcile the trace against the
authoritative rows in `public.telemetry`:

```bash
python tools/analyze_session.py --trace telemetry_trace.jsonl
python tools/export_messages.py   # writes session_messages.csv
python tools/chart_session.py     # writes session_charts.png
```

Two notes on interpreting the numbers:

- **Duplicates return HTTP 201.** The ingest Edge Function upserts with
  `onConflict: message_id, ignoreDuplicates: true`, so a re-sent `messageId`
  looks accepted to the device but inserts no new row. Duplicate counts must
  therefore be derived server-side, not from the device's status codes.
- **HTTP RTT and end-to-end delay are different quantities.** RTT comes from
  the Pi's `time.monotonic()` timer around the request; end-to-end is
  `created_at -` device timestamp and is only meaningful when both clocks are
  NTP-synchronised. Report RTT separately, and document clock sync before
  quoting end-to-end figures.

### Connection-loss & recovery

`tools/run_outage_trials.sh` black-holes **only** the Supabase ingest route
(Cloudflare ranges + resolved /32s — SSH and the LAN dashboard stay up) for three
5-minute trials, asserting the cut and restore and measuring how long the SQLite
store-and-forward buffer takes to drain. `tools/analyze_outage.py` reconciles the
resulting trace against Supabase. The 2026-10-16 results — **3 / 3 recovered,
0 messages dropped, 93 / 93 outage-scheduled messages stored** — are in
[`docs/measurements/connection_loss_report.md`](docs/measurements/connection_loss_report.md).

## Post-mitigation sessions (Wi-Fi/HTTPS re-test)

`tools/run_session.py` runs one measured session on the Pi: safety preflight
(86 °C hard limit, cool-down gate, disk, SSID, clock sync, buffer), a unique
`logs/runs/<UTC>_s<N>/` run folder, the full software/test manifest, 10 s
sampling, a short scripted alert/state-flap window, guaranteed teardown, and
postflight `get_throttled`/temperature readings. `tools/analyze_campaign.py`
then reconciles each trace with Supabase and reports per-session and combined
counts, queue high-water, cadence and RTT percentiles.

```bash
# on the Pi — one 30-minute session
python3 tools/run_session.py --session 1 --minutes 30 \
    --hotspot "SFM MBB - CONF" --flap-start-min 10 --flap-minutes 3

# afterwards, anywhere with network access
python tools/analyze_campaign.py /path/to/run_folder --out docs/measurements/post_mitigation
```

The flap window is driven by a **test-only** input, not by performing fatigue
actions in front of a driver. It is off unless both variables are set, and every
affected packet carries a `testInjection` marker:

```bash
ENABLE_TEST_STATE_INPUT=1
TEST_STATE_INPUT_PATH=/home/john/fatigue/logs/eval_state.txt
# file contents: NORMAL | DROWSY | CRITICAL_FATIGUE | ALCOHOL_ALERT
```

Remove both variables (and the drop-in the harness installs) after the run;
`run_session.py` does this itself and verifies the unit no longer sees them.
Method, artifacts and the claims this study does **not** support are documented in
[`docs/measurements/post_mitigation/README.md`](docs/measurements/post_mitigation/README.md).

### Camera-pipeline availability smoke test

The camera metrics recorded during the vehicle sessions were mostly unavailable
(0 %, 1.1 %, 0 %). A separate stationary test puts a consenting person in front of
the camera and logs **metrics only** — no frames, images or video:

```bash
# on the Pi; three announced phases: present, absent, present
python3 tools/run_camera_smoke.py --present-sec 120 --absent-sec 60

# locally
python tools/analyze_camera_smoke.py <run_dir> --out docs/measurements/camera_smoke
```

The app-side hook is off by default and enabled only by the harness:

```bash
CAMERA_METRICS_LOG_PATH=/home/john/fatigue/logs/camera_smoke/<run>/metrics.jsonl
```

It writes one JSON line per second (`face_present`, `frontal`, `ear`, `mar`,
`perclos`, `pitch`, `yaw`, `roll`, `driver_state`, `fps`). Result on 2026-10-22:
96.2–99.1 % face-metric availability with a face in view, 96.5 % correct absence,
~4.4 FPS with a face present. This is an availability check, **not** a
detection-accuracy claim; see
[`docs/measurements/camera_smoke/README.md`](docs/measurements/camera_smoke/README.md).

### GPS open-sky availability (one trial per power-on)

The receiver never achieved a fix in the in-vehicle sessions (0 % in all three).
A separate stationary power-cycle test was run on 2026-10-23: **2 / 3 power-on
trials fixed**, with derived power-on TTFF of **145.0 s and 211.3 s**; trial 3
timed out at 180 s. When fixed, the 30 s hold was 100 % valid (58 / 58), but only
**4 satellites** were used (HDOP 7.1–8.1), so this was a **marginal sky view**,
not a clean open-sky benchmark. The receiver did not acknowledge UBX, so these
are power-cycle starts, not verified receiver cold starts.

The harness is installed; each power-on runs one trial:

```bash
# one-time, on the Pi (installs the boot unit and arms 3 trials)
./fatigue-env/bin/python3 tools/run_gps_trials.py --install --arm \
    --target 3 --max-sec 180 --post-fix-sec 30

# then power off/on for each trial; wait for "DONE / POWER OFF" on the LCD
# after trial 3, pull the run folder and analyze locally:
python tools/analyze_gps_trials.py <run_dir> --out docs/measurements/gps_open_sky
```

Each trial stops after a 30 s continuous fix (or at the 180 s cap), writes
metrics only (no coordinates), and leaves `T<n> DONE <ttff>s / POWER OFF` on the
16×2 LCD. The harness records the Pi boot ID, uptime and `get_throttled` per
trial. See
[`docs/measurements/gps_open_sky/README.md`](docs/measurements/gps_open_sky/README.md).
Availability/TTFF only — no surveyed reference, so no position-accuracy claim.

## Known gaps vs. original project objectives

This implementation currently diverges from the original thesis objectives
in two ways, documented here for transparency:

- **Primary transport is Wi-Fi/cellular HTTPS (to Supabase), not literal
  GSM/GPRS hardware.** LoRa fallback logic is fully implemented and tested;
  an actual GSM/GPRS module (e.g. SIM800L) has not yet been integrated.
- **Dashboard is Supabase Realtime + a custom Flask live-view, not MQTT.**
  No MQTT broker is used in this implementation.

## License

Academic/thesis project. Add a license of your choice if open-sourcing.
