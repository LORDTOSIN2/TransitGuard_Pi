# Connection-stability measurements

Raw evidence for the study of the **Pi → Supabase ingest → Postgres → dashboard** upload path.
The narrative and the numbers are in [`stability_report.md`](stability_report.md); this file
explains the artifacts and how to reproduce them.

> **Three ~30-minute sessions** were run on 2026-10-16 on the `SFM MBB - CONF` hotspot.
> Combined totals: 612 scheduled, 580 attempted, 578 stored unique, 34 dropped (all in one
> alert burst in run 3), 99.66 % delivery vs attempted, HTTP RTT median 1.65 s / p95 3.79 s.
> The study also found and fixed a 49 % alert-message loss defect (`46077a2`).

> **A separate connection-loss & recovery study** — three 5-minute ingest outages on the same
> hotspot — is in [`connection_loss_report.md`](connection_loss_report.md):
> **3 / 3 trials recovered, 0 messages dropped, 93 / 93 outage-scheduled messages stored
> server-side**. Its artifacts use the `outage_*` / `telemetry_trace_outage.*` names.
> A **v2.1 recheck on 2026-10-22** (harness run end-to-end) reproduced this: 3 / 3 recovered,
> 0 dropped, **92 / 92** stored, 0 cut leaks —
> [`outage_recheck_v21/`](outage_recheck_v21/README.md).

> **A post-mitigation re-test** (three ~30-minute sessions with a scripted alert window, after
> the alert rate-limit and GPS/camera fixes) lives in
> [`post_mitigation/`](post_mitigation/): method, per-session artifacts and results.
> Combined: **571 / 571 scheduled messages stored, 0 dropped, 0 retried, queue high-water 1**,
> RTT median 1.53 s / p95 1.91 s / max 6.85 s. Three stationary runs on one hotspot — not a
> fleet or long-term reliability claim. The v2.1 outage recheck was run
> separately (see above).

> **A camera-pipeline availability smoke test** (face present / absent / present,
> metrics only, no video) is in [`camera_smoke/`](camera_smoke/):
> **96.2–99.1 % face-metric availability with a face in view**, 96.5 % correct
> absence, ~4.4 FPS with a face present. Availability only — no accuracy claim.
> The vehicle sessions' 0–1.1 % camera figures stand unchanged.

> **A GPS power-cycle availability test** (three power-on starts, stationary,
> antenna placed for sky view) is in [`gps_open_sky/`](gps_open_sky/):
> each power-on ran one short trial (180 s cap, 30 s after a fix), metrics only,
> no coordinates. **2 / 3 trials fixed** (power-on TTFF 145.0 s and 211.3 s);
> trial 3 timed out. When fixed, the 30 s hold was 100 % valid, but only **4
> satellites** were used (HDOP 7.1–8.1), so this was a **marginal sky view**, not
> a clean open-sky benchmark. UBX reset was not acknowledged, so these are
> power-cycle starts, not verified cold starts. Availability/TTFF only - no
> surveyed reference, so no position-accuracy claim.

## Naming

The measurement tools (`tools/analyze_session.py`, `tools/export_messages.py`,
`tools/chart_session.py`) **hardcode the canonical filenames** (`telemetry_trace.jsonl`,
`session_messages.csv`, …). The canonical files are therefore **run 3**; runs 1 and 2 carry a
`_run1` / `_run2` suffix. To analyse another run, copy it over the canonical name first.

| File | Run | Contents |
|---|---|---|
| `telemetry_trace.jsonl` | **3** | raw JSONL trace — `scheduled` / `attempt` / `dropped` events with monotonic timestamps, RTT, HTTP status, server date |
| `telemetry_trace_run1.jsonl` | 1 | raw trace |
| `telemetry_trace_run2.jsonl` | 2 | raw trace |
| `session_messages.csv` | **3** | one row per reconciled message |
| `session_messages_run1.csv` | 1 | reconciled rows |
| `session_messages_run2.csv` | 2 | reconciled rows |
| `session_summary.json` | **3** | machine-readable summary (counts, RTT percentiles, delivery ratios) |
| `session_summary_run1.json` | 1 | summary |
| `session_summary_run2.json` | 2 | summary |
| `session_charts.png` | **3** | latency + stability charts |
| `session_charts_run1.png` | 1 | charts |
| `session_charts_run2.png` | 2 | charts |
| `stability_report.md` | all | the written report |
| `connection_loss_report.md` | – | the connection-loss & recovery report (3 × 5 min outages) |
| `telemetry_trace_outage.jsonl` | – | raw trace for the three outage trials |
| `outage_poll.csv` | – | 5 s poll (buffer / network / temp / throttled / blackhole count) during the outages |
| `outage_summary.txt` | – | harness log: cut/restore assertions, per-trial recovery lines, leaks |
| `outage_leaks.txt` | – | cut-leak checks — **empty (0 lines) = no leak** |
| `outage_summary.json` | – | machine-readable per-trial outage summary (from `analyze_outage.py`) |

## Reproducing a session

```bash
# 1. On the Pi, enable tracing (add to /etc/transitguard.env, then restart transitguard):
TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace_runN.jsonl

# 2. After the session, copy the trace down and reconcile it against Supabase:
scp john@<pi>:/home/john/fatigue/logs/telemetry_trace_runN.jsonl telemetry_trace.jsonl
python tools/analyze_session.py --trace telemetry_trace.jsonl      # merges server-side rows
python tools/export_messages.py                                    # -> session_messages.csv
python tools/chart_session.py                                      # -> session_charts.png

# 3. IMPORTANT: remove TELEMETRY_TRACE_PATH and restart again.
#    Tracing grows ~5.8 MB/day and the Pi's disk runs at ~91 % used.
```

## Reproducing the connection-loss trials

```bash
# On the Pi: point tracing at a dedicated file via a systemd drop-in, then restart.
#   /etc/systemd/system/transitguard.service.d/zz-outage-test.conf
#   [Service]
#   Environment=TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace_trials2.jsonl
sudo systemctl daemon-reload && sudo systemctl restart transitguard

sh tools/run_outage_trials.sh        # 3 x (5 min ingest blackhole + up to 3 min observe)

# Pull outage_poll.csv / outage_summary.txt / outage_leaks.txt / telemetry_trace_outage.jsonl,
# then reconcile locally (prints the harness verdict AND the corrected verdict):
python tools/analyze_outage.py outage_poll.csv outage_summary.txt telemetry_trace_outage.jsonl

# Teardown: remove the drop-in, daemon-reload, restart, confirm TELEMETRY_TRACE_PATH is gone.
```

The harness black-holes **only** the Supabase ingest route (Cloudflare ranges + resolved /32s),
so SSH and the LAN dashboard stay up. LoRa is a point-to-point UART radio with no internet path
and is **not** part of this test.

## Caveats

* Duplicates are undetectable from the HTTP status: the ingest function upserts with
  `onConflict: message_id, ignoreDuplicates: true`, so a duplicate returns **HTTP 201** and
  inserts nothing. Duplicate counts are derived server-side.
* `HTTP_TIMEOUT_SEC` applies **per socket operation**, not per request, so multi-second
  requests can still succeed.
* HTTP round-trip (RTT) is authoritative; end-to-end delay additionally relies on NTP-synced
  clocks (the Pi's clock is UTC+1 in the journal, Supabase is UTC).

See [`../../CONTEXT.md`](../../CONTEXT.md) for the Pi layout, SSH access, hotspot switching and
Supabase details.
