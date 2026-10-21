# Post-mitigation Wi-Fi/HTTPS campaign

Method and artifacts for the **post-mitigation** re-test of the Pi → Supabase
telemetry path, run after the alert-publish rate limit (`46077a2`) and the
queue/gps/camera fixes. This directory holds the evidence; the narrative report
is [`report.md`](report.md).

## What is being tested

Three ~30-minute sessions on one named hotspot with the vehicle **stationary**:

* routine telemetry at the configured 10 s cadence for the whole session;
* one short, scripted **alert/state-flap interval** per session (3 min, starting
  10 min in) driven through the gated test-only state file — **no fatigue
  actions are performed in front of a driver**;
* no induced network outage in these sessions (that is the separate
  `connection_loss_report.md` study).

The scripted input is `ENABLE_TEST_STATE_INPUT` + `TEST_STATE_INPUT_PATH`
(`vehicle_monitor.py`, off by default). Every packet produced while it is active
carries a `testInjection` object, and the trace records `test_injected_state`,
so injected telemetry can never be mistaken for a measured detection.

## Safety preflight

`tools/run_session.py` refuses to start when:

* the SoC is **above 86 °C** (the test plan's hard limit) — checked again on
  every 10 s sample, aborting the session if it is crossed mid-run;
* the SoC has not fallen to the start limit (default 80 °C) after a bounded
  cool-down wait (default 10 min);
* free disk is below 100 MB;
* the clock is not NTP-synchronised (unless explicitly waived, in which case the
  limitation must be recorded);
* the active SSID is not the required hotspot;
* the ingest host is unreachable, `/data` has no heartbeat, or the offline
  buffer is not empty at start.

Before/after readings (`temp`, `get_throttled`, disk, memory, buffer, uptime) are
written to `preflight.json` / `postflight.json`; `get_throttled` is decoded into
its individual bits so an under-voltage run cannot be silently reported as clean.

## Artifacts per session

Unique run folders under `logs/runs/<UTC>_s<N>/`, so a restart can never
overwrite an earlier result:

| File | Contents |
|---|---|
| `manifest.json` | date/time/timezone, clock-sync status, app + model hashes, OS/kernel/Python/pip inventory, active SSID (never the password), RSSI, plan, cool-down record |
| `preflight.json` | safety readings before the test |
| `postflight.json` | safety readings after the test and after the teardown restart |
| `samples.csv` | 10 s poll: temp, throttled, queue depth, offline buffer, driver state, injected state, SSID/RSSI, GPS availability, camera-metric availability, heartbeat |
| `flaps.csv` | every scripted state write (epoch, elapsed, state) |
| `telemetry_trace.jsonl` | raw `scheduled` / `attempt` / `dropped` events |
| `summary.json` | harness counts + reconciliation results |
| `reconciled_messages.csv` | one row per scheduled message: attempts, last status, stored?, driver state, injection marker |

## Reconciliation and statistics

`tools/analyze_campaign.py` reconciles each trace against the authoritative
`public.telemetry` rows and writes `campaign_summary.json` plus
`campaign_tables.md`.

Reported per session **and combined**:

* **Counts** — scheduled, attempted, attempted unique, accepted HTTP 201,
  dropped, retried, scheduled-but-never-attempted, stored unique.
* **Ratios** — HTTP 201 acceptance, stored/attempted, stored/scheduled.
* **Queue** — high-water mark (in-memory queue, from both the trace and the
  10 s samples) and offline-buffer high-water.
* **Cadence** — median/mean/min/max gap between scheduled messages.
* **RTT** — min, median, mean, p90, p95, p99, standard deviation, max
  (request → response on the Pi's monotonic timer; **not** end-to-end).
* **Availability** — heartbeat, GPS fix percentage, camera-metric presence
  percentage, first-fix elapsed time if a fix occurs.
* **Safety** — SoC before/after/max, `get_throttled` before/after.

## Claims this study does not support

* **No fleet or long-term reliability claim** from three half-hour runs on one
  hotspot. Results are reported per session; combined figures are descriptive.
* **No detection-accuracy claim.** Camera availability measures whether the
  EAR/MAR/PERCLOS/pitch signals are present at all, not whether they are correct.
* **No GPS-accuracy claim.** Fix availability and (if achieved) time-to-first-fix
  are reported; there is no surveyed reference position.
* **Field effectiveness is not established** unless the mitigation is exercised
  in a real drive. If it is not, that limitation is retained in the report rather
  than describing the fix as effective.
