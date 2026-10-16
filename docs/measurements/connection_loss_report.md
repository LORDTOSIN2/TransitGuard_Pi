# TransitGuard — Connection-Loss & Recovery Report

**Device:** BUS-101 (Raspberry Pi 4, `vehicle_monitor.py` @ `668445a`)
**Path under test:** the Pi's **offline store-and-forward buffer** → Supabase ingest → Postgres
**Network:** SFM MBB - CONF (same hotspot as the connection-stability study)
**Method:** ingest-scoped WAN blackhole — SSH and the LAN dashboard are untouched
**Trials:** three consecutive 5-minute outages, 2026-10-16 13:53–14:18 (BST, +01:00)
**Result:** **3 / 3 trials recovered · 0 messages dropped · 93 / 93 outage-scheduled messages
stored server-side · 0 cut leaks**

> Companion to [`stability_report.md`](stability_report.md), which characterises the *healthy*
> path. This report characterises what happens when that path **disappears for 5 minutes** and
> then returns.

---

## 1. Objective and scope

Verify that a **sustained loss of the internet path to Supabase** does not lose telemetry:
every message scheduled while the uplink is down must be buffered locally and **replayed
successfully** once the uplink returns, with the buffer draining back to zero. The test also
measures how quickly recovery happens.

**Explicitly out of scope — LoRa.** The ATK-LORA-01 is a **point-to-point UART radio with no
internet path**. It was left connected (`/dev/ttyAMA2`) and was **not tested or isolated**. The
journal does show the application's LoRa fallback engaging during each outage, but no LoRa
measurement is claimed here.

---

## 2. Method

| Element | Implementation |
|---|---|
| Fault injection | `ip route add blackhole` for the Cloudflare ranges Supabase sits behind (`104.18.0.0/16`, `172.64.0.0/16`) **plus the resolved /32s**, re-resolved every 30 s during the cut |
| Scope | The **ingest route only** — LAN, SSH and the dashboard (all on-link) are never affected |
| Cut length | **300 s per trial**, on an absolute wall-clock deadline (no sampling drift) |
| Observation | **up to 180 s after restore**, stopping early once the buffer is empty **and** a fresh **live** HTTP 201 has landed after clearance |
| Cut assertion | after `cut`: probe must return `000` **and** ≥ 1 blackhole route must exist — printed per trial |
| Restore assertion | after `uncut`: probe must be non-`000` **and** blackhole routes must be `0`; any survivor is swept and reported |
| Safety net | a supervised transient unit (`tg-outage-deadman`) deletes the routes after 480 s even if the harness dies |
| Client authority | the gated telemetry trace (`TELEMETRY_TRACE_PATH`) — `scheduled` / `attempt` / `dropped` events with `source`, `ok`, `http_status`, `rtt_ms` |
| Server authority | Supabase `public.telemetry` (`message_id`, `created_at`) via PostgREST |

`rtt_ms` is measured on the Pi with `time.monotonic()` around each POST; `wall_epoch` is stamped
when the attempt **completes**.

### Recovery criterion

Recovery counts as confirmed only when **both** hold:

1. the SQLite buffer reaches **0**, **and**
2. a **live** (not replayed) HTTP **201** is recorded **after** the buffer emptied.

Requiring a *live* 201 after clearance — rather than just a drained buffer — proves the primary
uplink healed, not merely that buffered packets were being replayed.

---

## 3. Harness defects found and fixed

### 3.1 First run (v1) — discarded

The first attempt (12:36–13:07) produced `RECOVERY NOT CONFIRMED` for all three trials. It was
**void**, for three harness reasons:

| # | Defect | Fix |
|---|---|---|
| 1 | The "restore" step could leave the blackhole in place; the app only recovered after a **manual** route delete, so every observation window ran against a live blackhole | `uncut` removes the targeted routes **and sweeps any survivor**, then the harness **asserts** probe ≠ `000` and routes = 0 |
| 2 | Recovery was read from the poll CSV with `cut`, which returns empty in the script environment ⇒ `pending before = ?`, blank `throttled`/`sha256` | The CSV is parsed with `awk` only, and cut/restore are **asserted**, not assumed |
| 3 | Recovery detection counted the `[SENT via WiFi/Cellular]` journal line, which is emitted **only for live posts**, never for **flush replays** | Recovery is detected from the trace JSONL, counting `live` and `flush` 201s |

Because of (1) the windows were invalid, so **run 1 was discarded** and re-run with the hardened
harness.

### 3.2 This run (v2) — a verdict expression bug, corrected from the trace

The hardened harness ran cleanly, but its built-in **verdict text was wrong in all three
trials**. The early-stop test compared the **first** live-201 epoch against the clear time:

```sh
[ "$flive" -gt $((RESTORE_EPOCH+clear_at)) ]   # flive = FIRST live 201
```

In practice the first live 201 usually lands **while the buffer is still draining** (the uploader
is busy replaying), so it is *earlier* than the clearance time and the test could never fire —
even though live 201s kept arriving afterwards. Trial 1 shows it plainly:

```
TRIAL 1  recovery: first 201 at t+5s | first LIVE 201 at t+39s | queue first empty at t+89s | remaining=0
TRIAL 1  post-restore 201s: total=50 live=17
TRIAL 1  RECOVERY NOT CONFIRMED          <-- wrong
```

From the trace, trial 1's live 201s completed at (seconds after restore):
`37, 76, 94, 108, 110, 113, 117, 119, 123, 125, 127, 130, 134, 143, 152, 162, 178` — so
**15 live 201s landed after the queue emptied at +89 s**.

**Fix (`v2.1`, committed as `tools/run_outage_trials.sh`):** the test now uses the **last** live-ok
epoch, i.e. *"any live 201 after clearance"*. Only the verdict expression changed — no
measurement logic. Because the run itself used the v2 expression, the verdicts below are
**re-derived from the trace** by `tools/analyze_outage.py`, which reports both the harness label
and the corrected verdict.

Side effect: with the early-stop unable to fire, every observation window ran the full 180 s
instead of stopping early, so the run took 24 min 33 s instead of ~16 min. This **added** data
(more post-restore 201s) — it did not invalidate any of it.

### 3.3 Trial-2 "RESTORE INCOMPLETE" — a probe artefact, no data impact

Trial 2's restore probe returned `000` and the harness printed `!! RESTORE INCOMPLETE`, even
though `blackhole_routes=0`. This was a **probe transient**, not a stuck block:

| Evidence | Value |
|---|---|
| Blackhole routes at "restore" | **0** (asserted) |
| Next poll sample (t+5 s) | `network_online=True` |
| First live 201 after restore | completed at **t+2 s** |
| Post-restore failures in trial 2 | **0** |

The single 4-second `curl` fired immediately after the routes were deleted; the app's own POST
succeeded 2 s later. Trials 1 and 3 both show `restore verified (block lifted)`.

---

## 4. Results

### 4.1 Per trial

| | Trial 1 | Trial 2 | Trial 3 | **Total** |
|---|---|---|---|---|
| Cut (BST) | 13:53:54 | 14:02:02 | 14:10:16 | — |
| Restored (BST) | 13:59:00 | 14:07:09 | 14:15:18 | — |
| Outage duration | 306 s | 307 s | 302 s | 15.3 min |
| Cut verified (`000` + routes ≥ 1) | ✅ | ✅ | ✅ | 3 / 3 |
| Restore verified (probe ≠ `000`, routes = 0) | ✅ | ✅\* | ✅ | 3 / 3 |
| Buffer before outage | 0 | 0 | 0 | — |
| Peak buffered during outage | 32 | 30 | 30 | — |
| Buffered at restore | 32 | 30 | 30 | 92 |
| Scheduled during outage | 33 | 30 | 30 | **93** |
| Attempts during outage (all failed) | 63 | 60 | 61 | 184 |
| Replays (flush HTTP 201) after restore | 33 | 30 | 31 | **94** |
| **Stored server-side (of scheduled)** | **33 / 33** | **30 / 30** | **30 / 30** | **93 / 93** |
| Dropped (queue eviction) | **0** | **0** | **0** | **0** |
| Remaining at end | 0 | 0 | 0 | 0 |
| Time to first 201 | +5 s | +7 s | +9 s | — |
| Time to first **live** 201 | +39 s | +7 s | +40 s | — |
| Time to buffer = 0 | +89 s | +123 s | +102 s | — |
| Live 201s **after** clearance | 15 | 17 | 20 | 52 |
| Post-restore 201s (total / live) | 50 / 17 | 49 / 19 | 51 / 20 | 150 / 56 |
| Post-restore failures | 2 | 0 | 0 | **2** |
| Temp min–max | 66.2–76.4 °C | 74.5–78.8 °C | 77.4–80.8 °C | — |
| **Verdict (harness v2 text)** | NOT CONFIRMED | NOT CONFIRMED | NOT CONFIRMED | — |
| **Verdict (corrected, from trace)** | **CONFIRMED** | **CONFIRMED** | **CONFIRMED** | **3 / 3** |

\* Trial 2's `RESTORE INCOMPLETE` label is the probe artefact described in §3.3; the block was
verifiably lifted.

### 4.2 Recovery time

* The buffer was empty within **89–123 s** of the uplink returning, despite 30–32 queued
  messages (the uploader replays up to 15 per cycle).
* A **live** 201 followed clearance in every trial (15–20 occurrences), i.e. the primary path
  was genuinely healthy again, not merely draining.
* All 93 outage-scheduled messages were stored server-side. Time from restore to each message
  landing: **min 2 s, median ~40–75 s, max 116 s**.

### 4.3 Round-trip time (post-restore, Pi monotonic timer)

| Source | n | min | median | p95 | max |
|---|---|---|---|---|---|
| Replay (`flush`) | 94 | 1 537 ms | 2 003 ms | 7 003 ms | 8 120 ms |
| Live | 60 | 1 453 ms | 2 184 ms | 7 250 ms | 9 107 ms |
| **Combined** | **154** | **1 453 ms** | **2 021 ms** | — | **9 107 ms** |

The post-restore tail is heavier than the steady-state study (combined median 2 021 ms vs
1 647 ms; max 9 107 ms vs 17 020 ms) — expected while a backlog is draining over a just-restored
link. `HTTP_TIMEOUT_SEC = 6` applies **per socket operation**, which is why a 9.1 s request can
still succeed.

### 4.4 Supabase reconciliation

Across the reconciliation window (earliest cut − 60 s → latest restore + 400 s), **165 rows**
were fetched from `public.telemetry` for BUS-101. For each trial, the set of `message_id`s
scheduled during the outage was checked against the table:

```
trial 1: scheduled=33 unique=33 stored=33 missing=0
trial 2: scheduled=30 unique=30 stored=30 missing=0
trial 3: scheduled=30 unique=30 stored=30 missing=0
```

**Zero messages lost.** The 94th replay (trial 3) is a re-send of a message already stored; the
ingest function upserts with `ignoreDuplicates: true`, so it returns 201 and inserts nothing —
consistent with the duplicate-handling caveat in [`README.md`](README.md).

---

## 5. Store-and-forward evidence

The bounded offline buffer (`MAX_BUFFERED_PACKETS = 5000`) never came close to its cap: the peak
was **32**. During each outage the in-memory `telemetry_queue` (maxsize 50) also stayed well
below capacity, and the trace recorded **0 `dropped` events** across all three trials — the
failure mode seen in the stability study (alert-burst overflow) did not recur.

The evidence chain for a single outage is: **cut asserted → all live POSTs fail → messages
enqueue into SQLite → restore asserted → `flush_buffer()` replays them with HTTP 201s → buffer
reaches 0 → a live 201 confirms the primary path.** All 93 messages from the three outages
completed it.

---

## 6. Thermal and power notes

* Temperature across the run: **66.2 → 80.8 °C** (the Pi started cooled, ~64.7 °C, after a
  power-down requested before the run). Trial means: 71.7 / 76.6 / 79.3 °C.
* `get_throttled` stayed at **`0x50005`** throughout: under-voltage detected/occurred (bit 0 /
  bit 16) and *currently throttled* (bit 2) — but **no soft-temperature-limit bit** (bit 3 = 0),
  so the throttling is **power-driven** (the Pi runs on a battery), not thermal. Max temperature
  stayed below the 85 °C soft limit.
* The run finished at 80.8 °C; no reboot or Wi-Fi change was performed by the test.

---

## 7. Limitations

1. **One network, one link.** SFM MBB - CONF only; the behaviour may differ on a mobile link with
   a different loss/backoff profile.
2. **Synthetic outage shape.** A blackhole drops packets silently. A real outage could also
   involve re-DHCP, NAT changes or DNS failure. Only the ingest host was blackholed; DNS and the
   LAN stayed up, so DNS-failure paths were not exercised.
3. **Recovery verdict is trace-derived for this run.** The harness's own verdict expression was
   wrong (§3.2); verdicts here come from `tools/analyze_outage.py`. The committed harness (`v2.1`)
   fixes the expression but has **not** itself been run end-to-end — the correction is a strict
   widening of the same test over the same events.
4. **Load.** ~1 message / 10 s plus the alert path; a higher rate would change drain times
   (the uploader replays ≤ 15 packets per cycle).
5. **LoRa not measured.** The fallback fired (journal) but is out of scope (no internet path).
6. **Battery power.** Under-voltage throttling was active; it did not visibly affect the result
   (all 93 messages stored) but is a confounder for timing figures.
7. **`wall_epoch` = attempt completion**, so "time to first 201" is measured at response time.

---

## 8. Artifacts and reproduction

| File | Contents |
|---|---|
| `outage_poll.csv` | buffer / network / driver-state / temp / throttled / blackhole-count every 5 s, per trial/phase |
| `outage_summary.txt` | harness log: assertions, per-trial recovery lines, leaks, final state |
| `outage_leaks.txt` | cut-leak checks — **empty (0 lines)** |
| `telemetry_trace_outage.jsonl` | raw trace (`scheduled` / `attempt` / `dropped`); 13 KB predates the run (live posts), the rest is the run |
| `outage_summary.json` | machine-readable per-trial summary from `analyze_outage.py` |
| `tools/run_outage_trials.sh` | the harness (v2.1) |
| `tools/analyze_outage.py` | trace ↔ Supabase reconciler + corrected verdict |

```bash
# On the Pi (app dir), with tracing enabled via a systemd drop-in:
#   /etc/systemd/system/transitguard.service.d/zz-outage-test.conf
#   [Service]
#   Environment=TELEMETRY_TRACE_PATH=/home/john/fatigue/logs/telemetry_trace_trials2.jsonl
sudo systemctl daemon-reload && sudo systemctl restart transitguard

sh tools/run_outage_trials.sh            # ~25 min: 3 x (5 min cut + up to 3 min observe)

# Pull the artifacts, then reconcile locally:
python tools/analyze_outage.py outage_poll.csv outage_summary.txt telemetry_trace_outage.jsonl
```

**Teardown (required):** remove the drop-in, `daemon-reload`, restart `transitguard`, and confirm
`TELEMETRY_TRACE_PATH` is gone from the process environment — tracing grows ~5.8 MB/day and the
Pi's disk runs ~91 % full. (Done after this run; the drop-in was removed at 14:20:37.)
