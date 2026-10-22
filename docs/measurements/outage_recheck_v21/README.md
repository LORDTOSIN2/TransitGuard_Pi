# Ingest-outage recovery recheck — v2.1 harness

**Status: complete (2026-10-22).** This reruns the corrected v2.1 harness end to
end, which the 2026-10-16 study could not do: its verdict expression was wrong
and the results had to be re-derived from the trace.

**Result: 3 / 3 trials recovered · 0 messages dropped · 92 / 92 outage-scheduled
unique IDs stored · 0 cut leaks · buffer returned to 0 each time · live HTTP 201
after clearance in every trial.**

> **Scope caveat, unchanged:** this blackholes only the Supabase ingest route.
> Wi-Fi association, hotspot, DNS and DHCP recovery are **not** tested. Keep this
> limitation when citing the result.

## Conditions

| Item | Value |
|---|---|
| Date | 2026-10-22, 01:23–01:42 UTC |
| Device | BUS-101 (Raspberry Pi 4) |
| Network | `SFM MBB - CONF` (same hotspot as the stability and post-mitigation studies) |
| App | `910de89` — `vehicle_monitor.py` SHA-256 `4f0639929311…` (same file as the post-mitigation sessions) |
| Config | `/etc/transitguard.env` SHA-256 `288bd62f…` |
| Ingest host | `hfzqfcuezuxcgaifxqok.supabase.co` → `104.18.38.10`, `172.64.149.246` |
| Method | `ip route add blackhole` for `104.18.0.0/16`, `172.64.0.0/16` + resolved /32s |
| Timing | cut 300 s, observe ≤ 180 s, poll 5 s, deadman 480 s |
| Safety preflight | 58.4 °C, `get_throttled 0x50005`, 579 MB free, buffer 0, no leftover blackhole routes |
| Safety postflight | 74.0 °C, `get_throttled 0x50005`, no blackhole routes, trace drop-in removed and process env verified clear |
| Vehicle | stationary |

SSH and the LAN dashboard were never affected: both are on-link and the
blackhole covered only the ingest route.

## Per-trial results

| | T1 | T2 | T3 |
|---|---|---|---|
| Cut verified (probe `000`, ≥1 blackhole) | yes | yes | yes |
| Restore verified (probe non-`000`, 0 blackholes) | yes | yes | yes |
| Buffer before outage | 0 | 0 | 0 |
| Buffer peak during outage | 30 | 29 | 30 |
| Buffer at restore | 30 | 29 | 30 |
| Scheduled during outage | 31 | 30 | 31 |
| Unique queued | 31 | 30 | 31 |
| Unique stored server-side | **31** | **30** | **31** |
| Dropped from queue | 0 | 0 | 0 |
| First 201 after restore | +8 s | +6 s | +4 s |
| First **live** 201 | +33 s | +32 s | +27 s |
| Buffer reached 0 | +65 s | +65 s | +53 s |
| Live 201 **after** clearance | 1 | 5 | 5 |
| Harness verdict (v2.1) | CONFIRMED | CONFIRMED | CONFIRMED |
| Independent corrected verdict | CONFIRMED | CONFIRMED | CONFIRMED |
| SoC range during trial | 60–68 °C | 68–72 °C | 71–74 °C |

"First 201" is the first buffered packet replayed; the **live** 201 is a fresh
post-restore send, which is what proves the uplink healed rather than merely
draining a buffer.

## Totals

| Metric | Value |
|---|---|
| Trials | 3 |
| Cut verified / restore verified | 3 / 3 · 3 / 3 |
| Recovered (harness and independent) | **3 / 3** |
| Outage-scheduled unique IDs | 92 |
| Stored unique | **92 (100 %)** |
| Replayed from buffer | 91 (the 92nd was stored by a live attempt) |
| Dropped | **0** |
| Cut leaks (`outage_leaks.txt`) | **0 lines** |
| Blackhole routes at end | 0 |

## Interpretation

* The v2.1 verdict expression was exercised end to end. Harness and independent
  reconciliation agree on all three trials — the correction was a strict widening
  over the same events, and this run confirms it behaves as intended.
* The store-and-forward path held for three consecutive 5-minute ingest outages:
  no loss, no drops, and the buffer drained in 53–65 s.
* Power state was not clean: `get_throttled` remained `0x50005` (under-voltage,
  throttled) throughout and the SoC climbed from 58 °C to 74 °C. This is a
  confounder for timing, not for the delivery count.

## Not tested / not claimed

* **Wi-Fi, hotspot, DNS and DHCP recovery.** The cut was a route blackhole; the
  interface stayed associated and name resolution stayed up.
* **LoRa.** Point-to-point UART, no internet path; not exercised or measured.
* **Other links.** One hotspot only; a mobile link with different loss/backoff
  behaviour may recover differently.
* **Higher load.** ~1 message / 10 s plus the alert path; drain time scales with
  backlog and the uploader's replay rate.
* **Field effectiveness.** Stationary bench test with a synthetic outage.

## Artifacts

| File | Contents |
|---|---|
| `outage_poll.csv` | 5 s poll: buffer, network, driver state, temp, throttled, blackhole count |
| `outage_summary.txt` | harness log: cut/restore assertions, recovery lines, leaks, completion |
| `outage_leaks.txt` | cut-leak checks — **empty = no leak** |
| `telemetry_trace_trials2.jsonl` | raw `scheduled` / `attempt` / `dropped` trace (`dropped=0`) |
| `outage_summary.json` | machine-readable per-trial reconciliation from `tools/analyze_outage.py` |
| `outage_run_v21.log` | detached-run log |

Reproduce with `tools/run_outage_trials.sh` (v2.1) and
`tools/analyze_outage.py`; see
[`../connection_loss_report.md`](../connection_loss_report.md) §8 for the
drop-in and teardown steps.
