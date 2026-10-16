# TransitGuard — Telemetry Upload-Path Connection-Stability Report

**Device:** BUS-101 (Raspberry Pi 4, `vehicle_monitor.py` @ `9efe359` + `c53d1e3`)
**Path under test:** Pi → Supabase ingest Edge Function → Postgres → dashboard
**Network:** SFM MBB - CONF (same hotspot for all three runs, RSSI ≈ −65…−68 dBm)
**Sessions:** three consecutive ~30-minute runs, 2026-10-16

---

## 1. Objective

Measure the connection stability of the telemetry upload path and report:

- scheduled / attempted / accepted / failed / duplicate counts, per `messageId`
- alert-triggered messages counted separately
- HTTP round-trip time from a **monotonic** timer on the Pi (reported separately from
  end-to-end delay)
- delivery ratio = accepted unique messages / attempted messages

---

## 2. Method

| Element | Implementation |
|---|---|
| Instrumentation | Gated JSONL trace in the app (`TELEMETRY_TRACE_PATH`, **off by default**) writing `scheduled`, `attempt`, `dropped` events |
| RTT timer | `time.monotonic()` captured around each HTTP POST → `t_mono_start` / `t_mono_end` / `rtt_ms` |
| Server side | HTTP status code, any timeout/error, and server `Date` header captured per attempt |
| Authority | Supabase `public.telemetry.created_at` (server receive time) |
| Dedupe | Ingest function upserts `onConflict: message_id, ignoreDuplicates: true`, so **duplicates return HTTP 201 and insert nothing** — duplicate/repeat counts are therefore derived server-side, not from HTTP status |
| Clocks | Pi `systemd-timesyncd` (NTP, stratum 2) and Supabase's NTP-synced host; cross-checked against the HTTP `Date` header |
| Publish interval | 10 s (`PUBLISH_INTERVAL_SEC`) |

**Event semantics:** `scheduled` = queued by the main loop; `dropped` = evicted from the
bounded queue (drop-oldest) and therefore **never sent**; `attempt` = one HTTP POST.

---

## 3. Per-run counts (reconciled trace ↔ Supabase)

| | Run 1 | Run 2 | Run 3 | **Combined** |
|---|---|---|---|---|
| Window (UTC) | 04:19:06–04:49:31 | 06:21:47–06:52:03 | 07:04:13–07:34:18 | — |
| Duration | 30.41 min | 30.28 min | 30.10 min | 90.8 min |
| Scheduled | 179 | 192 | 241 | **612** |
| — of which `interval` | 179 | 173 | 172 | 524 |
| — of which `alert` | 0 | 19 | **69** | 88 |
| Dropped from queue | 0 | 0 | **34** | **34** |
| Attempted (HTTP POSTs) | 180 | 192 | 208 | **580** |
| Attempted, distinct `messageId` | 179 | 192 | 207 | **578** |
| Accepted (HTTP 201) | 179 | 192 | 207 | **578** |
| Stored unique server-side | 179 | 192 | 207 | **578** |
| Failed (non-201 / timeout) | 1 | 0 | 1 | **2** |
| Duplicate attempts (re-sends) | 1 | 0 | 1 | **2** |
| Scheduled but never attempted | 0 | 0 | **34** | **34** |
| 201 but not stored | 0 | 0 | 0 | 0 |
| **Delivery ratio vs attempted** | 99.44 % | **100.00 %** | 99.52 % | **99.66 %** |
| **Delivery ratio vs scheduled** | 100.00 % | 100.00 % | **85.89 %** | **94.44 %** |

Server-side confirmation (rows in each window): **179 / 192 / 207** — matching the
accepted unique counts exactly.

**Both failures were `ReadTimeout`s on the live path.** In each case the message was
re-attempted from the SQLite buffer and stored successfully, so **the two failures caused
zero data loss**. This also confirms the client-side buffer/retry path works.

---

## 4. Latency

### HTTP round-trip (Pi monotonic timer, request → response)

| | Run 1 | Run 2 | Run 3 | **Combined** |
|---|---|---|---|---|
| n (completed responses) | 179 | 192 | 207 | **578** |
| min | 1 273 ms | 1 382 ms | 1 359 ms | **1 273 ms** |
| median | 2 132 ms | 1 606 ms | 1 555 ms | **1 647 ms** |
| mean | 2 822 ms | 1 741 ms | 1 671 ms | **2 051 ms** |
| p90 | — | — | — | **3 020 ms** |
| p95 | 7 229 ms | 2 326 ms | 2 068 ms | **3 788 ms** |
| p99 | — | — | — | **7 989 ms** |
| max | 17 020 ms | 6 446 ms | 6 090 ms | **17 020 ms** |
| stdev | 2 019 ms | 506 ms | 417 ms | **1 293 ms** |
| attempts > 5 s | 18 | 0 | 1 | **19** |
| attempts > 10 s | 3 | 0 | 0 | **3** |

### End-to-end delay (server `created_at` − device timestamp)

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| n | 180 | 192 | 208 |
| median | 1 918 ms | 1 353 ms | 1 346 ms |
| p95 | 7 793 ms | 3 118 ms | 4 521 ms |
| max | 16 912 ms | 6 728 ms | 11 536 ms |

End-to-end is reportable because both clocks are NTP-synced; the HTTP `Date` header
cross-check put the Pi **+214 ms / −101 ms / −127 ms** relative to the Supabase edge
(Date header resolution is 1 s, so these are coarse). **RTT is the authoritative latency
figure**; end-to-end additionally includes queue wait.

---

## 5. Stability over time (5-minute buckets)

| Run 1 | Run 2 | Run 3 |
|---|---|---|
| 30 / 29 / 29 / 31 / 29 / 30 attempts, 1 failure; median RTT **1 542 → 2 472 ms (drifting up)** | 32 / 29 / 30 / 29 / 41 / 29 attempts, 0 failures; median RTT flat **1 567–1 771 ms** | 59 / 30 / 29 / 29 / 30 / 30 attempts, 1 failure; median RTT flat **1 503–1 653 ms** |

Scheduling cadence median: **10.22 s / 10.23 s / 10.22 s** vs 10 s configured.

The latency tail is **time-varying**, not constant: run 1's p95 (7 229 ms) is ~3× run 3's
(2 068 ms) on the same network at similar RSSI. Reporting a single tail figure from any one
run would be misleading — the combined p95 of 3 788 ms is the more defensible number.

---

## 6. Key finding — alert-burst queue overflow (**data loss**)

Run 3 exposed a real defect. The driver entered `CRITICAL_FATIGUE`/`DROWSY` for ~70 s
(07:05:40–07:06:50 UTC) and generated **69 alert-triggered messages**, 60 of them inside a
21-second window.

**Root cause — app-side, not network-side:** while `alert_active`, the main loop schedules a
message on **every iteration (~20 FPS)**, but the single uploader thread drains only one at a
time at ~1.5 s per POST. The `telemetry_queue` (maxsize 5, drop-oldest) therefore saturated
and stayed pinned at depth 5 for ~21 s (50 of 69 alert samples sat at q=5), evicting the
oldest messages continuously.

Evidence that this is **not** a connectivity problem: RTT during the drop window was
**normal** (n=27, median 1 535 ms, max 2 891 ms) with **no HTTP failures**.

Impact:

| Metric | Value |
|---|---|
| Alert-triggered messages scheduled | 69 |
| Alert-triggered messages **dropped** | **34** |
| Alert-path loss rate | **49 %** |
| Interval-path loss rate | **0 %** (0 of 172 dropped) |
| Worst-case data loss (run 3, vs scheduled) | **14.1 %** |

All 34 dropped messages were alert-triggered; not one interval message was lost.

**Fix applied (commit `46077a2`) and validated by simulation:**

1. **Rate-limited the alert path** — the minimum gap before the next send is
   `ALERT_PUBLISH_MIN_INTERVAL_SEC` (5 s) while an alert is active or a state transition
   just occurred, otherwise `PUBLISH_INTERVAL_SEC` (10 s). Entering an alert is still
   reported promptly (after a quiet NORMAL stretch the elapsed gap already exceeds the
   shorter floor), and rapid `DROWSY`↔`CRITICAL_FATIGUE` flapping — observed every
   ~0.4 s in run 3 — can no longer storm the queue.
2. **Raised `telemetry_queue` maxsize 5 → 50** (`TELEMETRY_QUEUE_MAXSIZE`) as a safety
   margin; the rate limit is the primary control.
3. **Observability** — `alert_coalesced` is exposed in `/data`, and `state_changed` /
   `alert_coalesced` are added to the trace's `scheduled` event.

Simulated against run 3's exact flapping pattern (70 s of alternating states every 0.4 s,
uploader draining 1 per 1.5 s):

| | sends | drops | max queue depth | lost |
|---|---|---|---|---|
| OLD (maxsize 5) | 1 406 | 1 350 | 5 | 96 % |
| **NEW (5 s floor, maxsize 50)** | **20** | **0** | **1** | **0 %** |

The fix is a producer-side rate limit, so it does not alter the network measurement: the
interval cadence remains 10 s (verified after deploy).

---

## 7. GPS (recorded for completeness)

`has_fix = false` and `satellite_count = 0` in every run — as expected indoors. After commit
`c53d1e3` the module reports GGA/GSV, showing **0–2 satellites in view at a best C/N0 of
29 dBHz** (a fix needs ≥ 4 satellites at ≈35–40 dBHz). This is an antenna/sky-view
limitation and did **not** affect the upload-path measurements: the `location` object is
still present and the schema accepts null `latitude`/`longitude`.

---

## 8. Limitations

1. **Single network.** All three runs used the same hotspot; the other hotspot (Redmi 14C)
   was not compared. Results characterise this link only.
2. **Time-varying tail.** Tail latency differs ~3× run-to-run; treat the combined p95
   (3 788 ms) as the estimate, not any single run's.
3. **Alert workload not uniform.** Runs 1/2/3 contained 0/19/69 alert messages, so the runs
   are not identical workloads — which is precisely what revealed the queue defect.
4. **End-to-end uses two clocks.** Both are NTP-synced but not perfectly aligned
   (±100–200 ms); RTT is the authoritative figure.
5. **Duplicate detection is server-derived**, since the ingest function returns 201 for
   deduplicated inserts.
6. `HTTP_TIMEOUT_SEC = 6` is applied per **socket operation**, not per request, which is why
   multi-second requests can still complete successfully.

---

## 9. Artifacts

| File | Contents |
|---|---|
| `telemetry_trace.jsonl` | run 3 raw trace (canonical name) |
| `telemetry_trace_run1.jsonl`, `_run2.jsonl` | runs 1–2 raw traces |
| `session_messages.csv` | run 3 reconciled per-message rows (208) |
| `session_messages_run1.csv`, `_run2.csv` | runs 1–2 per-message rows |
| `session_summary.json` | run 3 machine-readable summary |
| `session_summary_run1.json`, `_run2.json` | runs 1–2 summaries |
| `session_charts.png` / `_run1.png` / `_run2.png` | latency + stability charts |
| `analyze_session.py` | trace ↔ Supabase reconciler |
| `chart_session.py` | chart renderer (needs `matplotlib`) |
| `export_messages.py` | per-message CSV exporter |

All of the above live in this directory (`docs/measurements/`) in the `TransitGuard_Pi`
repo. The **un-suffixed names are run 3** — they are the canonical filenames that
`analyze_session.py`, `export_messages.py` and `chart_session.py` hardcode, which is why
the raw traces are stored under those names.

## 10. Reproduction

```bash
# On the Pi, add to /etc/transitguard.env (then restart transitguard):
TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace_runN.jsonl

# After the session, copy the trace and reconcile:
scp john@<pi>:/home/john/fatigue/logs/telemetry_trace_runN.jsonl telemetry_trace.jsonl
python analyze_session.py --trace telemetry_trace.jsonl
python export_messages.py
python chart_session.py
# Remember to remove TELEMETRY_TRACE_PATH afterwards (it grows ~5.8 MB/day).
```
