# Integrated outdoor test (GPS + fatigue + alcohol + packet delivery)

This folder documents the **three-session integrated run** requested for the paper:
one power-on, three short outdoor sessions, while the normal TransitGuard app and
its local dashboard remain running. It combines the sensors already used by the
separate GPS, camera and alcohol checks rather than creating a second, competing
sensor stack.

## Current status

The indoor smoke runs validated sampling, LCD updates, trace switching and
Supabase reconciliation plumbing; they are not sensor or delivery evidence.
Two outdoor integrated campaigns were completed on 2026-10-23. The first
obtained no fresh GPS fix during its sessions. The second used the fresh-fix
gate added after that failure, waited 8 min 41 s for the fix, and then recorded
three sessions with 100% fresh-fix availability in each.

See the compiled [integrated test report](./report.md) and the
[privacy-redacted outdoor results](./results/). Exact latitude/longitude
columns and local SSID/IP values are retained only in the local pulled copies
under `raw/`; they are intentionally omitted from the public results.

The second run also recorded active undervoltage/throttling (`0x50005`) and a
peak SoC temperature of 81.3 C. Review the report's caveats before interpreting
these runs; the follow-up campaign does not establish GPS accuracy or prove the
cause of the first run's lack of fix.

## What one run does

One invocation (or one armed boot), after the GPS gate is satisfied, runs:

```
20 s minimum pre -> 150 s session 1 -> 30 s gap
          ->  150 s session 2  ->  30 s gap
          ->  150 s session 3  ->  10 s post
```

The pre phase waits for the GPS gate even if its minimum 20 seconds have
elapsed. The wait is bounded by `--gps-wait-sec` (default 900 seconds). Each
session stays in the short 2-3 minute range used for this test. The app stays
in charge of the camera, GPS UART, ADS1115/MQ-3,
buzzer and LCD. The harness only polls the app's existing `GET /data` endpoint
once per second, so it cannot take the serial port or camera away from the app.

During the sessions the 16x2 LCD shows:

```
T1/3 132s GPS4
ALC 7.4 FACE
```

and at the end:

```
3/3 DONE
PULL LOGS
```

The normal app can overwrite the overlay when its driver state changes; the
harness refreshes it every 5 s. The overlay is **local-only** and does not alter
driver state or telemetry.

## Boot workflow (recommended)

The unit is already installed and enabled on the Pi, but it is **inert** until a
state file is armed, so ordinary power cycles are unaffected.

```bash
ssh john@john.local
cd /home/john/fatigue

# 1. Refresh the installed unit if the harness was updated, then arm the next
#    power-on. The GPS gate prevents session 1 from starting without a fresh fix.
./fatigue-env/bin/python3 tools/run_integrated_test.py --install
./fatigue-env/bin/python3 tools/run_integrated_test.py --arm \
    --sessions 3 --session-sec 150 --gap-sec 30 --boot-delay 60 \
    --gps-wait-sec 900 --label outdoor2

# 2. Power off, move the Pi/antenna outdoors, power on.
#    Wait for the fresh-fix confirmation, then "3/3 DONE / PULL LOGS" on the LCD.
#    Dashboard: http://john.local:8080

# 3. Optional: cancel an armed run before power cycling.
./fatigue-env/bin/python3 tools/run_integrated_test.py --disarm
```

A successful run writes `BOOT_STATE.done.json` and removes `BOOT_STATE.json`;
later power cycles are normal. Other failed runs leave the state armed so a
retry is possible. A GPS wait timeout is recorded as an aborted run, writes the
completion marker, disarms the campaign, and starts no measured sessions;
correct the antenna/sky-view or power issue before explicitly arming a new run.

The boot unit is:

```
tg-integrated-test.service
ConditionPathExists=/home/john/fatigue/logs/integrated/BOOT_STATE.json
```

It runs after `transitguard.service`, as user `john`, and does not stop or
replace the app. It has no network-online dependency: if the Pi is outdoors with
no reachable network, the sessions still record to the local SD card and the LCD
still shows progress. Packet uploads may fail and buffer; the message-ID
reconciliation can be run later after reconnecting.

## Manual run

If the Pi is already outdoors and reachable over SSH, start the same plan now:

```bash
cd /home/john/fatigue
./fatigue-env/bin/python3 tools/run_integrated_test.py \
    --sessions 3 --session-sec 150 --gap-sec 30 --gps-wait-sec 900 \
    --label outdoor-manual
```

The fresh-fix gate also applies to manual runs.

The harness refuses to start above `--max-start-temp` (default 80 C), above the
hard 86 C limit, with less than 100 MB free, without a camera device, or if the
app is not active. It stops a session if the SoC crosses `--max-temp` (default
86 C). This is consistent with the other measurement harnesses: **do not start a
run on a Pi above 86 C, and cool it between sessions/trials.**

## What is recorded

Each run gets a unique folder under `logs/integrated/run_<UTC>_<label>/`:

| File | Contents |
|---|---|
| `manifest.json` | app commit/hash, model hash, boot ID, network, env keys (secrets redacted), plan, trace path |
| `preflight.json` | temperature, throttling, disk, camera devices, SSID/IP, heartbeat, clock sync |
| `phases.json` | exact UTC/epoch boundaries for pre, sessions, gaps and post |
| `samples.csv` | 1 Hz samples from `GET /data` |
| `telemetry_trace.jsonl` | app `scheduled` / `attempt` / `dropped` telemetry events with message IDs |
| `delivery_local.json` | local scheduled/attempted/HTTP-201/failed/dropped counts and per-session breakdown |
| `summary.json` | per-session and overall summaries, caveats and non-claims |
| `postflight.json` | service/data state, temperature, throttling, trace cleanup |
| `delivery_supabase.json` | written later by the analyzer; message-ID-to-Supabase reconciliation |

Public result copies omit precise coordinates, local SSIDs/IPs, and per-message
telemetry IDs. The original pulled logs remain in the local `raw/` directories
for authorized analysis. See [report.md](./report.md) for the published
per-session comparison and links to the sanitized summaries, samples and
harness logs.

The sample columns cover:

- **GPS:** latitude/longitude, `fix_valid`, `using_last_known`,
  `location_age_sec`, satellites used/in view, best SNR, fix quality, HDOP,
  altitude and speed. The summary reports fresh-fix percentage, median
  satellites/HDOP, and fix-to-fix reported distance. That distance is **not**
  surveyed ground truth and is not a position-accuracy result.
- **Fatigue:** `face_present`, `frontal`, EAR, MAR, PERCLOS, pitch, driver state.
  No frames, images or video are written. The run does not deliberately induce
  fatigue and does not make an accuracy claim.
- **Alcohol:** app ratio, app estimated ppm and the app's alert/caution
  thresholds. The MQ-3 is a relative vapor sensor; this is not a breathalyzer,
  BAC/BrAC or calibrated concentration measurement.
- **System/network:** buffer count, telemetry queue, network online, temperature,
  throttling, buzzer and normal app LCD text.
- **Packet delivery:** see below.

## Packet delivery

The app already has an optional per-event trace, disabled in normal operation.
For a traced run, the harness temporarily installs:

```
/etc/systemd/system/transitguard.service.d/zz-integrated-trace.conf
[Service]
Environment=TELEMETRY_TRACE_PATH=<run_dir>/telemetry_trace.jsonl
```

and restarts the app once. This is the only app restart during the run: it
happens **before** the sessions. The app then writes one `scheduled` event and
one or more `attempt` events per telemetry message, including:

- `messageId` (unique in `public.telemetry.message_id`);
- `wall_epoch` and `device_ts_ms`;
- HTTP status, RTT, server date, error;
- queue size and source (`live` or `flush`).

After the sessions the drop-in is removed and the app is restarted again, so
normal operation is restored. The dashboard is down only during those two
restarts, not during the recorded sessions. The boot unit and the app are left
running.

The local counts are not yet proof of a row. Reconcile the message IDs against
Supabase (from the Pi or after pulling the run folder):

```bash
python3 tools/analyze_integrated_test.py \
    --run-dir logs/integrated/run_<UTC>_<label> \
    --supabase-url https://<project-ref>.supabase.co \
    --supabase-key <publishable/anon key> \
    --device-id BUS-101
```

Use the **public publishable/anon key**, not a service-role key. The script
looks up the scheduled message IDs directly in chunks (falling back to a
time-window query if necessary), matches `message_id`, and writes
`delivery_supabase.json`, including:

- scheduled/attempted/HTTP-201/failed/dropped counts;
- found/missing message IDs and per-session delivery ratio;
- approximate latency from the HTTP attempt start to the Supabase row's
  `created_at` (device clock and request/response time apply);
- explicit notes about buffering and the query grace window.

A negative-looking end-to-end value from the response time is normal because the
Edge Function inserts the row before it returns; the analyzer therefore uses
the attempt start, not the response end.

## Disabling tracing / manual alternative

For a run where the app must not be restarted at all, use `--no-trace`. The
sample/LCD/GPS/fatigue/alcohol recording still works, but packet tracing and
message-ID matching are unavailable; only `/data` buffer/queue values remain.

Normal app operation has tracing off. Never leave the trace drop-in installed
after the campaign: tracing grows on a Pi whose disk is about 91 % full.

## Non-claims and caveats

This is an **end-to-end availability/function run**, not a controlled
metrology experiment:

- GPS points are recorded as reported; there is no surveyed reference, so no
  position-accuracy claim. Antenna placement/sky view and the marginal receiver
  results from `gps_open_sky/` still apply until a clean-sky repeat is done.
- The camera metrics are availability/metrics only; no video is stored, no
  fatigue is induced, and there is no clinical or detection-accuracy claim.
- The MQ-3 ratio is relative. It is not calibrated ppm, BAC or BrAC. Do not
  compare it numerically with another sensor without a reference gas.
- Three sessions in one power-on are not three independent placements or a
  fleet/long-term reliability claim. Do not pool these sessions with the
  separate workload/placement runs.
- Under-voltage/throttling (`get_throttled`) must be reported; a run is not
  "clean" if the bitmask is non-zero.
- The original local `samples.csv` contains **GPS coordinates**. Published
  result copies blank the latitude/longitude columns; the separate GPS
  availability campaign deliberately retained no coordinates. No video or
  images are retained.
- Network delivery can be delayed by the SQLite buffer. A message missing from
  the query window may arrive later; the report says so and should not be read
  as permanent loss without checking the buffer.

## Related files

- `tools/run_integrated_test.py` - harness, boot unit, LCD overlay and local
  trace summary.
- `tools/analyze_integrated_test.py` - Supabase message-ID reconciliation.
- `vehicle_monitor.py` - local-only `POST /test/lcd` overlay endpoint, LCD lock,
  and added `/data` fields (`hdop`, `altitude_m`, `speed_kmh`).
- `tools/run_gps_trials.py`, `tools/run_camera_smoke.py`,
  `tools/record_alcohol_test.py` - the separate, narrower tests whose caveats
  still apply.
- `docs/measurements/gps_open_sky/`, `camera_smoke/`, `alcohol_smoke/` - the
  separate evidence folders.
