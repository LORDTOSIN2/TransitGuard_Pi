# Post-mitigation Wi-Fi/HTTPS re-test — interim results

**Status: 2 of 3 sessions complete (2026-10-21).** Session 3 is pending; this
file is updated after each. Method, artifacts and non-claims are in
[`README.md`](README.md).

The mitigation under test is the alert-publish rate limit (`46077a2`) plus the
GPS/camera busy-spin fix (`c53d1e3`), re-measured on the upload path
(Pi → Edge Function → Postgres). No ingest outage is induced in these sessions.

## Common conditions

| Item | Value |
|---|---|
| Hotspot | `SFM MBB - CONF` (single named hotspot) |
| Pi address | 10.80.3.194 |
| App | `c2ad776` (working tree dirty only with untracked `logs/` and helper scripts) |
| `vehicle_monitor.py` SHA-256 | `4f0639…8b29` |
| `face_landmarker.task` SHA-256 | `64184e…c9ff` |
| OS / kernel / Python | Debian 12 (bookworm) / `6.12.93+rpt-rpi-v8` / 3.11.2 |
| Clock | NTP synchronised, `Africa/Lagos` (UTC+1) |
| Vehicle | stationary, routine 10 s telemetry throughout |
| Scripted window | +10 min, 3 min, state written every 20 s, then override released |

Each session runs in its own `raw/<UTC>_s<N>/` folder with its own
`manifest.json`, `preflight.json`, `postflight.json`, `samples.csv`,
`flaps.csv`, `telemetry_trace.jsonl`, `summary.json` and
`reconciled_messages.csv`.

## Session 1 — 22:16:36–22:46:36 UTC

| Metric | Value |
|---|---|
| Scheduled / attempted / attempted unique | 190 / 190 / 190 |
| Stored unique server-side | 190 |
| Dropped / retried / never attempted | 0 / 0 / 0 |
| HTTP 201 acceptance | 100.00 % |
| Stored per attempted / per scheduled | 100.00 % / 100.00 % |
| Queue high-water | 1 message (offline buffer 0) |
| Cadence | median 10.02 s, mean 9.39 s, min 4.95 s, max 10.53 s |
| RSSI at start | −76 dBm |
| SoC before / after / max | 75.0 / 78.4 / 79.4 °C |
| `get_throttled` before / after | `0x50005` / `0x50005` |

## Session 2 — 23:23:34–23:53:34 UTC

| Metric | Value |
|---|---|
| Scheduled / attempted / attempted unique | 192 / 192 / 192 |
| Stored unique server-side | 192 |
| Dropped / retried / never attempted | 0 / 0 / 0 |
| HTTP 201 acceptance | 100.00 % |
| Stored per attempted / per scheduled | 100.00 % / 100.00 % |
| Queue high-water | 1 message (offline buffer 0) |
| Cadence | median 10.02 s, mean 9.31 s, min 4.96 s, max 10.10 s |
| RSSI at start | −78 dBm |
| SoC before / after / max | 60.9 / 77.4 / 77.9 °C |
| `get_throttled` before / after | `0x50005` / `0x50005` |

Session 2 started after a 37-minute cool-down (61 °C at launch) and reached a
lower peak than session 1. Teardown was verified against the running process:
`TELEMETRY_TRACE_PATH` and `ENABLE_TEST_STATE_INPUT` were both absent afterwards.

## Delivery and queue, per session and combined

| Metric | Run 1 | Run 2 | Combined |
|---|---|---|---|
| Scheduled | 190 | 192 | 382 |
| Attempted (HTTP POSTs) | 190 | 192 | 382 |
| Attempted unique | 190 | 192 | 382 |
| Stored unique (server) | 190 | 192 | 382 |
| Dropped from queue | 0 | 0 | 0 |
| Retried | 0 | 0 | 0 |
| Scheduled, never attempted | 0 | 0 | 0 |
| HTTP 201 acceptance, % | 100.00 | 100.00 | 100.00 |
| Stored / attempted, % | 100.00 | 100.00 | 100.00 |
| Stored / scheduled, % | 100.00 | 100.00 | 100.00 |
| Queue high-water | 1 | 1 | 1 |
| Duration, min | 30.0 | 30.0 | 60.0 |

Combined ratios are computed from the totals, not averaged from the two
percentages. They are descriptive of these two runs, not a reliability claim.

## HTTP round-trip time (response-only, Pi monotonic timer)

| RTT | Run 1 | Run 2 | Combined |
|---|---|---|---|
| n | 190 | 192 | 382 |
| min | 1356.2 | 1406.2 | 1356.2 |
| median | 1478.0 | 1566.9 | 1526.8 |
| mean | 1502.8 | 1758.4 | 1631.3 |
| p90 | 1604.9 | 1926.7 | 1744.7 |
| p95 | 1673.0 | 2178.9 | 1952.4 |
| p99 | 1895.2 | 6651.6 | 6525.4 |
| stdev | 105.4 | 826.7 | 603.8 |
| max | 2245.8 | 6850.8 | 6850.8 |

**Session 2 has a heavy tail:** five attempts took 6.5–6.9 s, spread across
23:27–23:40 UTC and not clustered in the scripted window. All returned HTTP 201,
so they are latency outliers, not losses. The median moved only ~89 ms between
sessions while p99 moved by ~4.8 s — the link, not the queue, is responsible.
`HTTP_TIMEOUT_SEC` applies per socket operation, which is why a 6.85 s request
can still succeed (documented caveat, unchanged).

This is **not** end-to-end delay.

## Availability (10 s samples, n = 182 per session)

| Signal | Run 1 | Run 2 |
|---|---|---|
| `/data` heartbeat present | 100.0 % | 100.0 % |
| GPS fix available | 0.0 % | 0.0 % |
| GPS time-to-first-fix | not achieved | not achieved |
| Camera metric present (`ear`/`mar`/`perclos`/`pitch`) | 0.0 % | 1.1 % |

In session 2 the camera pipeline produced all four metrics for two consecutive
samples (23:28:14–23:28:24 UTC) while classifying `DROWSY`, then went unavailable
again. That is a **signal-availability** observation, not a detection-accuracy
result: there is no reference measurement of the driver's actual eye state.

GPS never achieved a fix in either session (`sats_used = 0`), so no
time-to-first-fix can be reported.

## Safety

| Reading | Run 1 | Run 2 |
|---|---|---|
| SoC before | 75.0 °C | 60.9 °C |
| SoC after | 78.4 °C | 77.4 °C |
| SoC max sampled | 79.4 °C | 77.9 °C |
| `get_throttled` before | `0x50005` | `0x50005` |
| `get_throttled` after | `0x50005` | `0x50005` |
| Offline buffer at start | 0 | 0 |
| Free disk at start | 579.3 MB | 579.0 MB |

`0x50005` decodes to **under-voltage NOW + currently throttled**, with
under-voltage and throttling having occurred earlier in the boot. Both sessions
stayed below the 86 °C abort limit, but neither is a clean-power run and neither
is described as one.

## Interpretation, limited to what two sessions support

* Across 60 minutes and 382 scheduled messages on this hotspot, every scheduled
  message was accepted and stored (382/382), with zero queue drops and zero
  retries. The scripted alert/transition window did not push the queue above one
  message in either session.
* The pre-fix failure mode (a single alert burst dropping 34 messages) did not
  reproduce in either post-fix session.
* Latency is dominated by a stable base (~1.5 s median) with occasional
  multi-second outliers on this hotspot; session 2 shows the tail can reach
  ~6.9 s while still succeeding.
* Two half-hour sessions are still not evidence of fleet or long-term
  reliability, and nothing here establishes field effectiveness in a real drive.

## Measurement notes

* **Session 1 window.** Session 1 used the first harness version, whose teardown
  removed the systemd drop-in without restarting the service. The old process
  kept tracing for ~2.3 min, leaving 205 events in the file. The window
  `22:16:36–22:46:36 UTC` was reconstructed from `harness.log`, and the
  reconciler clips to it, giving 190 in-session messages. The 15 post-window
  messages are excluded from every figure above. This defect is fixed: session 2
  restarted on teardown and verified the environment was gone.
* **Duplicates.** The ingest function upserts with `ignoreDuplicates`, so a
  re-send returns 201 and stores nothing. Stored counts come from the
  `telemetry` table.
* **Scripted input.** Every packet produced during the flap windows carries
  `testInjection.state` in `raw_payload`, and the trace records
  `test_injected_state`. Those messages are test stimulus, not detections.

## Pending

* Session 3 (30 min, same hotspot, same 3-minute scripted window).
* Final combined table across all three sessions.
* Optional v2.1 outage-recovery recheck; `tools/run_outage_trials.sh` on the Pi
  matches the fixed v2.1 version. Its canonical artifact names mean the
  2026-10-16 files must be archived on the Pi before that run.
