# Post-mitigation Wi-Fi/HTTPS re-test — results

**Status: 3 of 3 sessions complete (2026-10-21/22).** Method, artifacts and
non-claims are in [`README.md`](README.md); generated tables are in
[`campaign_tables.md`](campaign_tables.md).

The mitigation under test is the alert-publish rate limit (`46077a2`) plus the
GPS/camera busy-spin fix (`c53d1e3`), re-measured on the upload path
(Pi → Edge Function → Postgres). No ingest outage was induced in these sessions;
the separate v2.1 recovery recheck was run afterwards and is reported under
*Outage-recovery recheck* below.

## What was tested

Three consecutive ~30-minute sessions on a single named hotspot, vehicle
stationary, routine telemetry at the configured 10 s cadence throughout. Each
session included one 3-minute scripted alert/state-flap window starting 10
minutes in, driven through the gated test-only state file — **no fatigue actions
were performed in front of a driver**.

## Common conditions (from each session's manifest)

| Item | Value |
|---|---|
| Hotspot | `SFM MBB - CONF` (all sessions) |
| Pi address | 10.80.3.194 |
| App code | `vehicle_monitor.py` SHA-256 `4f0639929311…` — **identical in all three sessions** |
| Git HEAD during run | `c2ad776` (s1), `910de89` (s2, s3); only harness/docs changed between them |
| Working tree | dirty only with untracked `logs/` and helper scripts |
| Model | `face_landmarker.task` SHA-256 `64184e229b26…` |
| OS / kernel / Python | Debian 12 (bookworm) / `6.12.93+rpt-rpi-v8` / 3.11.2 |
| Packages | 54 (recorded in each `manifest.json`) |
| Clock | NTP synchronised, `Africa/Lagos` (UTC+1) |
| SoC limit enforced | 86 °C abort; start gate 75–80 °C, bounded cool-down wait |
| Vehicle | stationary (no drive) |

Sessions were separated by a real cool-down: s1→s2 ≈ 37 min, s2→s3 ≈ 29 min.
Start temperatures were 75.0, 60.9 and 66.2 °C.

## Delivery and queue reconciliation — per session and combined

| Metric | Run 1 | Run 2 | Run 3 | Combined |
|---|---|---|---|---|
| Window (UTC) | 22:16:36–22:46:36 | 23:23:34–23:53:34 | 00:22:53–00:52:53 | — |
| Scheduled | 190 | 192 | 189 | **571** |
| Attempted (HTTP POSTs) | 190 | 192 | 189 | **571** |
| Attempted unique | 190 | 192 | 189 | **571** |
| Accepted HTTP 201 | 190 | 192 | 189 | **571** |
| Stored unique (server) | 190 | 192 | 189 | **571** |
| Dropped from queue | 0 | 0 | 0 | **0** |
| Retried | 0 | 0 | 0 | **0** |
| Scheduled, never attempted | 0 | 0 | 0 | **0** |
| HTTP 201 acceptance, % | 100.00 | 100.00 | 100.00 | **100.00** |
| Stored / attempted, % | 100.00 | 100.00 | 100.00 | **100.00** |
| Stored / scheduled, % | 100.00 | 100.00 | 100.00 | **100.00** |
| Queue high-water (msgs) | 1 | 1 | 1 | **1** |
| Offline-buffer high-water | 0 | 0 | 0 | **0** |
| Cadence median / mean | 10.02 / 9.39 s | 10.02 / 9.31 s | 10.02 / 9.44 s | — |
| Cadence min / max | 4.95 / 10.53 s | 4.96 / 10.10 s | 4.95 / 10.08 s | — |
| Duration | 30.0 min | 30.0 min | 30.0 min | 90.0 min |

The ~5 s cadence minimum is the alert-path floor (`ALERT_PUBLISH_MIN_INTERVAL_SEC
= 5`) during the scripted window, not a cadence fault. Combined ratios are
computed from totals, not by averaging percentages.

## Response-only HTTP RTT (Pi monotonic timer, request → response)

| RTT (ms) | Run 1 | Run 2 | Run 3 | Combined |
|---|---|---|---|---|
| n | 190 | 192 | 189 | 571 |
| min | 1356.2 | 1406.2 | 1286.8 | 1286.8 |
| median | 1478.0 | 1566.9 | 1527.0 | 1527.0 |
| mean | 1502.8 | 1758.4 | 1584.9 | 1615.9 |
| p90 | 1604.9 | 1926.7 | 1754.1 | 1753.4 |
| p95 | 1673.0 | 2178.9 | 1809.8 | 1911.2 |
| p99 | 1895.2 | 6651.6 | 2032.7 | 3823.1 |
| stdev | 105.4 | 826.7 | 386.5 | 541.7 |
| max | 2245.8 | 6850.8 | 6527.2 | 6850.8 |

**The tail is the story, not the median.** Six of 571 requests (1.05 %) exceeded
3 s: five in run 2 (6.5–6.9 s, scattered 23:27–23:40 UTC) and one in run 3
(6.53 s at 00:37:01 UTC). All six returned HTTP 201, so they are latency
outliers, not losses. Median moved by only ~89 ms across sessions while p99
ranged from 1.9 s to 6.7 s. `HTTP_TIMEOUT_SEC` applies per socket operation,
which is why a 6.85 s request can still succeed.

This is **not** end-to-end delay; it excludes queue wait and clock skew.

## Signal availability (10 s samples, n = 182 per session)

| Signal | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| `/data` heartbeat present | 100.0 % | 100.0 % | 100.0 % |
| GPS fix available | 0.0 % | 0.0 % | 0.0 % |
| GPS time-to-first-fix | not achieved | not achieved | not achieved |
| Camera metric present (`ear`/`mar`/`perclos`/`pitch`) | 0.0 % | 1.1 % | 0.0 % |

GPS never reported a fix in any of these three in-vehicle sessions
(`sats_used = 0`, `fix_quality = 0`), so **no time-to-first-fix exists** for this
campaign. In run 2 the camera pipeline
produced all four metrics for two consecutive samples (23:28:14–23:28:24 UTC)
while classifying `DROWSY`, then went unavailable again.

These in-vehicle figures are **unchanged**. A separate stationary smoke test on
2026-10-22 ([`../camera_smoke/`](../camera_smoke/README.md)) put a consenting
operator in front of the same camera (gated metrics-logging build `1ecd75dcb646…`,
off by default): with a face in view the pipeline produced valid face metrics for
**96.2–99.1 %** of seconds, and reported no face for **96.5 %** of the
face-absent phase, at **~4.4 FPS with a face present**. That shows the live camera
path works when a face is visible; it does **not** explain the 0–1.1 % campaign
values (face not in view, lighting, mounting or distance could each contribute),
and those campaign measurements stand as recorded.

These are **availability** observations. No detection-accuracy claim is made:
there is no reference measurement of the driver's actual eye/mouth state, and no
surveyed GPS reference position.

A separate stationary GPS power-cycle test was run on 2026-10-23
([`../gps_open_sky/`](../gps_open_sky/README.md)): three full Pi power cycles,
antenna placed for sky view, metrics only, 180 s cap. **2 / 3 trials fixed**
(derived power-on TTFF 145.0 s and 211.3 s); trial 3 timed out. When fixed, the
30 s post-fix hold was 100 % valid (58 / 58), but only **4 satellites** were used
(HDOP 7.1–8.1) and the whole-window fix-valid fraction was 12.7 % (58 / 456).
This is a **marginal, placement-limited** result, not a clean open-sky benchmark;
it does not change the in-vehicle 0 % figures above. The receiver did not
acknowledge `UBX-MON-VER`/`UBX-CFG-RST`, so these are power-cycle starts, not
verified cold starts, and under-voltage/throttling (`0x50005`) was present.

## Safety readings — before and after each session

| Reading | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| SoC before | 75.0 °C | 60.9 °C | 66.2 °C |
| SoC after | 78.4 °C | 77.4 °C | 77.9 °C |
| SoC max sampled | 79.4 °C | 77.9 °C | 77.9 °C |
| `get_throttled` before | `0x50005` | `0x50005` | `0x50005` |
| `get_throttled` after | `0x50005` | `0x50005` | `0x50005` |
| Offline buffer at start | 0 | 0 | 0 |
| Free disk at start | 579.3 MB | 579.0 MB | 578.7 MB |
| Mem available at start | 7295.5 MB | 7235.7 MB | 7253.7 MB |

`0x50005` decodes to **under-voltage NOW + currently throttled**, with
under-voltage and throttling having occurred earlier in the boot. Every session
stayed below the 86 °C abort limit, but **none is a clean-power run** and none is
described as one. A 6.85 s RTT tail and the camera dropouts are plausible
symptoms of the same power/thermal constraint; this campaign does not isolate
that cause.

## Interpretation, limited to this campaign

* Across 90 minutes and 571 scheduled messages on one hotspot, every scheduled
  message was accepted and stored (571/571), with zero queue drops and zero
  retries.
* The pre-fix failure mode — a single alert burst dropping 34 messages in run 3
  of the 2026-10-16 study — **did not reproduce**. The scripted alert window never
  pushed the in-memory queue above one message.
* Latency is a stable base (~1.5 s median) with rare multi-second outliers
  (1.05 % > 3 s). The outliers are link-side, not queue-side: the queue never
  held more than one message and there were no retries.
* This is **not** evidence of fleet or long-term reliability. Three half-hours on
  one hotspot are three observations.
* Field effectiveness is **not** established: these are stationary bench runs
  with scripted input, not a real drive.

## Minimum-results checklist

| Requested result | Status |
|---|---|
| Delivery/queue reconciliation + RTT stats, per run and combined | **Done** — tables above; machine-readable in `campaign_summary.json` |
| Trace-verified outage recovery from v2.1 | **Done** — 3/3 trials recovered, 0 dropped, 92/92 stored; see below |
| GPS fix availability and time-to-first-fix, no accuracy claim | **Done** — in-vehicle 0 % (no TTFF); separate stationary power-cycle test 2/3 fixed, power-on TTFF 145 / 211 s, 4 sats, HDOP 7.1–8.1 (marginal sky view) |
| Camera-pipeline signal availability, no accuracy claim | **Done** — campaign 0 %, 1.1 %, 0 %; separate face-present smoke test 96.2–99.1 %, no accuracy claim |
| Software/test manifest + safety pre/post readings | **Done** — per-session `manifest.json`, `preflight.json`, `postflight.json`, summarised above |

## Measurement notes

* **Session 1 window reconstruction.** Session 1 ran with the first harness
  version, whose teardown removed the systemd drop-in without restarting the
  service. The old process kept tracing for ~2.3 min, leaving 205 events in the
  file. The window `22:16:36–22:46:36 UTC` was reconstructed from `harness.log`
  and the reconciler clips to it, giving 190 in-session messages; the 15
  post-window messages are excluded from every figure. Fixed for sessions 2–3:
  teardown now restarts and verifies the variables are absent from
  `/proc/<pid>/environ` (`trace var in process=False`).
* **Duplicates.** The ingest function upserts with `ignoreDuplicates`, so a
  re-send returns 201 and stores nothing. Stored counts come from the
  `telemetry` table.
* **Scripted input.** Every affected packet carries `testInjection.state` in
  `raw_payload`; verified server-side, e.g. run 2 at 23:33:37 UTC stored
  `driverState = CRITICAL_FATIGUE` with
  `testInjection = {source: test-state-input, enabled: true, …}`, while the
  pre-write frame at 23:33:32 UTC was `NORMAL` with no marker. The trace also
  records `test_injected_state`.

## Outage-recovery recheck (v2.1, 2026-10-22)

Run separately from the three sessions, after they finished and the Pi had cooled
(58.4 °C at start). The fixed **v2.1** harness was exercised end-to-end rather
than only being deployed, which the 2026-10-16 study could not do because its
verdict expression was wrong.

Three 300 s ingest-scoped blackholes on the same hotspot, 5 s polling, ≤180 s
observation. LAN, SSH and the dashboard stayed up; only the Supabase route was
blocked.

| | Trial 1 | Trial 2 | Trial 3 | **Total** |
|---|---|---|---|---|
| Cut / restore verified | ✅ / ✅ | ✅ / ✅ | ✅ / ✅ | 3 / 3 |
| Buffer peak → at restore | 30 → 30 | 29 → 29 | 30 → 30 | 89 |
| Scheduled during outage | 31 | 30 | 31 | **92** |
| Stored server-side (unique) | **31 / 31** | **30 / 30** | **31 / 31** | **92 / 92** |
| Dropped | 0 | 0 | 0 | **0** |
| First **live** 201 after restore | +33 s | +32 s | +27 s | — |
| Buffer back to 0 | +65 s | +65 s | +53 s | — |
| Harness / independent verdict | CONFIRMED | CONFIRMED | CONFIRMED | **3 / 3** |

* **92 / 92** outage-scheduled unique IDs stored, **0** dropped, **0** cut leaks.
  91 were confirmed replayed from the buffer; the 92nd was stored by a live
  attempt — all 92 are accounted for server-side.
* The buffer emptied within **53–65 s**, with live 201s after clearance in every
  trial. Faster than the 2026-10-16 drain (89–123 s), but the runs differ in
  under-voltage state and link conditions, so this is not a claimed speed-up.
* Harness and the trace↔Supabase reconciler agree on all three trials, confirming
  the v2.1 verdict fix behaves as intended.
* Safety: 58.4 → 74.0 °C, `get_throttled 0x50005` throughout (under-voltage,
  so still not a clean-power run), 579 MB free, all blackhole routes removed and
  the trace drop-in removed with the process environment verified clear.

**Caveat, unchanged:** this does **not** test Wi-Fi association, hotspot, DNS or
DHCP recovery. The cut was a route blackhole over a healthy link. As with the
2026-10-16 study, a real outage that involves re-association or DNS failure is
still untested.

Artifacts: [`../outage_recheck_v21/`](../outage_recheck_v21/README.md) (poll CSV,
harness summary, empty leaks file, raw trace, `outage_summary.json`, run log);
full write-up in [`../connection_loss_report.md`](../connection_loss_report.md) §9.

## Reproduce

```bash
# on the Pi, one 30-minute session
python3 tools/run_session.py --session 4 --minutes 30 \
    --hotspot "SFM MBB - CONF" --flap-start-min 10 --flap-minutes 3 \
    --max-start-temp 75

# anywhere with network access
python tools/analyze_campaign.py logs/runs/<run_dir> --out docs/measurements/post_mitigation
```
