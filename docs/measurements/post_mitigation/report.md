# Post-mitigation Wi-Fi/HTTPS re-test — interim results

**Status: 1 of 3 sessions complete (2026-10-21).** Sessions 2 and 3 are pending;
this file is updated after each. Method, artifacts and non-claims are in
[`README.md`](README.md).

The mitigation under test is the alert-publish rate limit (`46077a2`) plus the
GPS/camera busy-spin fix (`c53d1e3`), re-measured on the upload path
(Pi → Edge Function → Postgres). No ingest outage is induced in these sessions.

## Session 1 — 2026-10-21 22:16:36–22:46:36 UTC (30.0 min)

| Item | Value |
|---|---|
| Run folder | `raw/20261021T221632Z_s1/` |
| Hotspot / RSSI | `SFM MBB - CONF` / −76 dBm at start |
| Pi address | 10.80.3.194 (SFM subnet) |
| App | `c2ad776` (working tree dirty: untracked `logs/`, helper scripts only) |
| `vehicle_monitor.py` SHA-256 | `4f0639…8b29` |
| `face_landmarker.task` SHA-256 | `64184e…c9ff` |
| OS / kernel / Python | Debian 12 (bookworm) / `6.12.93+rpt-rpi-v8` / 3.11.2 |
| Clock | NTP synchronised, `Africa/Lagos` (UTC+1), offset +3600 s |
| Vehicle | stationary (no drive), routine 10 s telemetry throughout |
| Scripted alert window | +10 min, 3 min, 9 state writes (20 s apart), then override released |

### Delivery and queue reconciliation

| Metric | Value |
|---|---|
| Scheduled | 190 |
| Attempted (HTTP POSTs) | 190 |
| Attempted unique | 190 |
| Stored unique (server) | 190 |
| Dropped from in-memory queue | 0 |
| Retried | 0 |
| Scheduled but never attempted | 0 |
| HTTP 201 acceptance | 100.00 % |
| Stored / attempted | 100.00 % |
| Stored / scheduled | 100.00 % |
| Queue high-water mark | 1 message |
| Offline-buffer high-water | 0 |
| Cadence | median 10.02 s, mean 9.39 s, min 4.95 s, max 10.53 s |

The 4.95 s minimum is the alert-path floor (`ALERT_PUBLISH_MIN_INTERVAL_SEC = 5`)
during the scripted window, not a cadence fault.

### HTTP round-trip time (request → response, Pi monotonic timer)

n = 190; all attempts returned HTTP 201, so there are no failure samples.

| min | median | mean | p90 | p95 | p99 | stdev | max |
|---|---|---|---|---|---|---|---|
| 1356.2 ms | 1478.0 ms | 1502.8 ms | 1604.9 ms | 1673.0 ms | 1895.2 ms | 105.4 ms | 2245.8 ms |

This is **not** end-to-end delay. The distribution is tight (σ ≈ 105 ms), which
is consistent with a stable link rather than a queueing backlog: the queue never
held more than one message.

### Availability (10 s samples, n = 182)

| Signal | Result |
|---|---|
| `/data` heartbeat present | 100.0 % |
| GPS fix available | 0.0 % (no fix at any sample; first fix never achieved) |
| Camera metric present (`ear`/`mar`/`perclos`/`pitch`) | 0.0 % |

Neither availability result is an accuracy statement. The camera pipeline
produced **no** EAR/MAR/PERCLOS/pitch values at all during this session — the
dashboard's `--` is truthful, and the driver-state field is therefore driven by
the fallback classification, not by measured eye/mouth geometry. GPS likewise
never reported a fix, so no time-to-first-fix exists for this run.

### Safety readings

| Reading | Before | After | Max sampled |
|---|---|---|---|
| SoC temperature | 75.0 °C | 78.4 °C | 79.4 °C |
| `get_throttled` | `0x50005` | `0x50005` | — |
| Free disk | 579.3 MB | — | — |
| Mem available | 7295.5 MB | — | — |
| Offline buffer | 0 | 0 | 0 |

`0x50005` decodes to **under-voltage NOW + currently throttled**, with
under-voltage and throttling having occurred earlier in the boot. The session
stayed below the 86 °C abort limit, but it is **not** a clean-power run and must
not be reported as one. The 30-minute cool-down gate was satisfied immediately
(74 °C at gate time).

## Interpretation, limited to what one session supports

* On this hotspot, in this session, the post-mitigation publish path accepted and
  stored **every** scheduled message (190/190), with zero queue drops and zero
  retries. That is a materially different outcome from the pre-fix run 3, where a
  single alert burst dropped 34 messages.
* The scripted 3-minute alert/transition window did **not** overflow the queue;
  the high-water mark was one message. This exercises the rate-limit fix under
  alert load without performing fatigue actions.
* Nothing here supports a fleet or long-term reliability claim. One half-hour
  session on one hotspot is a single observation.

## Measurement notes

* **Measurement window.** Session 1 was run with the first harness version,
  whose teardown removed the systemd drop-in but did **not** restart the service.
  The old process kept tracing for ~2.3 minutes after the session, so the trace
  file contains 205 events. The window `22:16:36–22:46:36 UTC` was reconstructed
  from `harness.log` and the reconciler clips to it, giving 190 in-session
  messages. The 15 post-window messages are excluded from every figure above.
  The harness now restarts on teardown and verifies the variable is gone from
  `/proc/<pid>/environ`, so sessions 2–3 will not have this defect.
* **Duplicates.** The ingest function upserts with `ignoreDuplicates`, so a
  re-send returns 201 and stores nothing. Stored counts above come from the
  `telemetry` table, not from HTTP status.
* **Scripted input.** Every packet produced during the flap window carries
  `testInjection.state` in `raw_payload`, and the trace records
  `test_injected_state`. These messages are test stimulus, not detections.

## Pending

* Session 2 and session 3 (30 min each, same hotspot, same 3-minute scripted
  window at +10 min).
* Combined table across the three sessions, per-session first.
* Optional v2.1 outage-recovery recheck (trace-verified), if it is run.
* Manifest and safety pre/post readings are already captured per session under
  `raw/<run>/`.
