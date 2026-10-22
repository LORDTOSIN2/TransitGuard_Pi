# GPS open-sky availability — method, artifacts and results

**Question:** after a power-cycle start, can the NEO-6M acquire a valid GPS fix
and hold it while the receiver is stationary with a clear view of the sky?

**Claim scope:** fix availability and time-to-first-fix (TTFF) only. There is no
surveyed reference position here, so this study makes **no position-accuracy
claim**. It is also not a moving-vehicle, fleet, or long-term reliability test.

> **Status (completed 2026-10-23):** all three power-on trials completed, with
> three distinct boot IDs. **2 / 3 acquired a fix**; trial 3 timed out at 180 s.
> TTFF was long (derived power-on TTFF 145.0 s and 211.3 s), and only **4
> satellites** were used (HDOP 7.1–8.1). When a fix was present it held for the
> full 30 s post-fix window (58 / 58 samples), but the low satellite count and
> long TTFF indicate a **marginal sky view**, not a strong open-sky benchmark.
> See [Results](#results--run_20261022t232744z) and the caveats below.

## Pilot probe (not one of the three trials)

A 60 s probe was run on 2026-10-22T23:24:59Z while the Pi was indoors:

- NMEA stream healthy: **416 lines / 60 s** (53 GGA, 120 GSV, 59 RMC), 9600 baud.
- **No fix**: `sats_in_view` 0–2, best C/N0 34 dBHz, `sats_used` 0.
- `UBX-MON-VER` got no response and the NMEA gap after `UBX-CFG-RST` was
  0.1 s: this receiver/clone did not acknowledge UBX, so the software cold
  start was **not observed**. The power cycle is therefore the primary start
  control, but it is **not a verified receiver cold start**; the UBX command is
  only a secondary control.
- Temperature 69.6–72.5 °C; `get_throttled 0x50005`.

This is consistent with an obstructed sky view, not a dead UART: the serial
path and NMEA parser work. Raw pilot files are in
[`raw/pilot_20261022T232459Z/`](raw/pilot_20261022T232459Z/). They are **not**
included in the three-trial availability result.

## Results — run_20261022T232744Z

**Setup:** armed 2026-10-22T23:27:44Z; trials started 2026-10-23T00:08:42Z,
00:13:15Z and 00:18:06Z (UTC). One trial per full Pi power cycle, stationary,
app stopped, 180 s cap, 30 s post-fix hold. Raw files:
[`raw/run_20261022T232744Z/`](raw/run_20261022T232744Z/);
machine-readable summary:
[`run_20261022T232744Z_summary.json`](run_20261022T232744Z_summary.json);
tables:
[`run_20261022T232744Z_tables.md`](run_20261022T232744Z_tables.md).

| Trial | Boot ID | Fix within 180 s | TTFF (reset) | Power-on TTFF | Whole-window fix-valid | Post-fix 30 s | Sats used (max in view) | HDOP | Best C/N0 med (max) | `get_throttled` end |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `dc2abd39` | yes, quality 1 | 88.39 s | 144.99 s | 25.7 % (29 / 113) | 100 % (29 / 29) | 4 (4) | 7.1 | 33 (34) dBHz | `0x50005` |
| 2 | `3715d0b1` | yes, quality 1 | 147.22 s | 211.32 s | 16.8 % (29 / 173) | 100 % (29 / 29) | 4 (4) | 8.1 | 34 (37) dBHz | `0x50000` |
| 3 | `13306e89` | **no** | — | — | 0.0 % (0 / 170) | n/a | 0 (4) | — | 28 (31) dBHz | `0x50005` |

Combined:

| Measure | Value |
|---|---|
| Trials with a fix within 180 s | **2 / 3** |
| Trials holding the fix for the 30 s post-fix window | **2 / 3** |
| Power-on TTFF min / median / max | **144.99 / 178.16 / 211.32 s** |
| Whole-window fix-valid fraction (includes pre-fix time) | **12.7 %** (58 / 456) |
| Post-fix fix-valid fraction | **100 %** (58 / 58), but only ~30 s each |
| Distinct boot IDs | **3 / 3** |
| UBX reset observed | **0 / 3** (NMEA gaps 0.11–0.33 s) |
| Any trial aborted | no |
| Coordinates stored | no |

**Interpretation.** Trial 1 fixed after 88.4 s (145.0 s after power-on); trial 2
after 147.2 s (211.3 s after power-on); trial 3 did not fix within 180 s. When a
fix was present it was stable: no losses, `sec_since_last_fix` ≤ 0.3 s, and all
29 samples in each 30 s hold were valid. That is useful short-term evidence, not
a steady-state availability figure.

The limiting factor is sky view, not the UART: only **four satellites were used**
(HDOP 7.1–8.1), and `sats_in_view` never exceeded four. A clean open-sky view
would be expected to show more satellites and better geometry; these numbers are
**placement-limited**. Treat this run as a marginal/lower-bound result, not a
representative open-sky benchmark, and do not pool it with a later run under
different antenna conditions. If a clean open-sky figure is needed, repeat the
campaign after checking the antenna connector, ground plane and sky view.

The power-on TTFF values are derived as `pi_uptime_at_start_s + TTFF (reset)`
(56.6 + 88.39 s; 64.1 + 147.22 s). The first logged sample was invalid in both
trials, so no fix was present at trial start. The receiver was powered from the
Pi, but the UBX reset was not acknowledged; these are **power-cycle starts**, not
verified receiver cold starts. Under-voltage/throttling was present
(`0x50005` at arm and at the end; trial 2 ended at `0x50000`), so this is not a
clean-power run. `0x50000` means the flags occurred but were not current at the
final sample; `0x50005` includes current under-voltage/throttling. SoC stayed at
46.7–59.9 °C across the three trials (the app was stopped during each test).

## Why a power-cycle per trial

The receiver has never achieved a fix in the vehicle sessions (0 % in all three
post-mitigation sessions; the module streamed NMEA with `fix_quality 0`,
0 satellites used, and 0–2 in view). The likely cause is antenna/sky view, not
the Pi UART. This test removes the vehicle and workload and was intended to give
the receiver an open-sky view. In this run the placement still yielded only four
satellites, so it remained marginal (see Results).

Each trial is one **full Pi power cycle**, so the receiver is power-cycled too.
The harness also sends a receiver-level cold-start command (`UBX-CFG-RST`,
`navBbrMask=0xFFFF`, `resetMode=0x02`) at the start of the trial. The trial
summary records both bases:

- `pi_uptime_at_start_s` and a distinct `boot_id` per trial (power-cycle
  evidence), and
- the NMEA gap after the UBX reset (whether the software reset was observed).

The pilot probe showed that this receiver/clone **did not acknowledge UBX**: no
`UBX-MON-VER` response and a 0.1 s NMEA gap after the reset. The power cycle is
therefore the only start control, and it is a **power-cycle start, not a
verified receiver cold start**: the NEO-6M backup domain may retain
almanac/ephemeris across the Pi power cycle. Report TTFF accordingly:

- `TTFF (reset)` is from the UBX command to the first valid GGA fix.
- `Power-on TTFF` is `pi_uptime_at_start_s + TTFF (reset)` when the reset was
  not acknowledged and no fix was present at trial start. If a fix was already
  present, only an upper bound (the uptime) can be reported.
- Do not describe these as strictly cold starts unless the reset gap confirms a
  receiver reset (it did not here).

## Trial definition

| Item | Setting |
|---|---|
| Placement | stationary, antenna in open sky, no vehicle roof |
| Duration cap | **180 s** per trial (3 min) |
| Early stop | after a valid GGA fix has been held for **30 s** |
| Start | full Pi power cycle; `UBX-CFG-RST` is also attempted (not acknowledged by this receiver) |
| Sampling | 1 Hz |
| Trials | 3 power-ons (target 3) |
| App state | `transitguard` stopped during the trial (serial is exclusive); LCD shows the result |
| Data | metrics only — **no coordinates stored** |

TTFF is measured from the reset command to the first GGA with `gps_qual > 0`;
where the reset was not acknowledged, the analyzer also reports **Power-on TTFF**
= Pi uptime at trial start + TTFF. Fix-valid fraction is reported both over the
whole trial window (which includes the cold-start interval, so it is not a
steady-state availability estimate) and over the post-fix window. Satellite
counts, best C/N0, HDOP, fix losses and the age of the last fix are recorded for
context only.

## Workflow

### One-time deploy and arm (over SSH, while the Pi is on)

```bash
scp tools/run_gps_trials.py tools/analyze_gps_trials.py \
    john@<pi-ip>:/home/john/fatigue/tools/

ssh john@<pi-ip>
cd /home/john/fatigue
./fatigue-env/bin/python3 tools/run_gps_trials.py \
    --install --arm --target 3 --max-sec 180 --post-fix-sec 30

# then put the Pi/antenna in open sky and power-cycle for trial 1
```

`--install` writes and enables `/etc/systemd/system/tg-gps-trial.service`.
`--arm` creates `logs/gps_trials/run_<UTC>/` and `BOOT_STATE.json`.
The next power-on runs trial 1. (`--start-now` is available if the Pi is
already in open sky and you do not want to power-cycle; it starts the unit
immediately.)

The unit waits 20 s after `transitguard` starts (so the normal app/LCD boot is
visible), stops the app to free `/dev/serial0`, runs one trial, writes the
result, and leaves the app stopped so the LCD result stays visible. SSH and the
network remain available.

### Each power-on

1. Put the Pi and antenna in open sky before powering on.
2. Power on. The app loads normally; after ~20 s the test starts.
3. The LCD shows `T<n>/3 GPS`, then `waiting <s>`, then `T<n> FIX <s>`.
4. When the LCD shows **`T<n> DONE <ttff>s` / `POWER OFF`**, power the Pi off.
   Wait for `DONE`: if you power off during the 30 s hold, that trial is not
   counted and the same trial number is retried on the next boot.
5. Repeat for trials 2 and 3. After trial 3 the LCD shows `ALL 3 DONE / SSH TO
   PULL`; the state is disarmed, so later boots are normal.

If the LCD shows `T<n> NO FIX` / `POWER OFF`, the trial completed with no fix;
it is still recorded (0 % availability). Power off and continue, or re-arm a new
campaign after fixing the antenna placement.

If the LCD shows `ABORTED` or `ERROR`, the app was left stopped. The error is in
the journal (`journalctl -u tg-gps-trial`) and SSH is available. The same trial
will be retried on the next boot because the state was not advanced.

### Pull and analyze

```bash
scp -r john@<pi-ip>:/home/john/fatigue/logs/gps_trials/run_<UTC> \
    docs/measurements/gps_open_sky/raw/
scp john@<pi-ip>:/home/john/fatigue/logs/gps_trials/BOOT_STATE.done.json \
    docs/measurements/gps_open_sky/raw/run_<UTC>/ 2>/dev/null || true

python tools/analyze_gps_trials.py \
    docs/measurements/gps_open_sky/raw/run_<UTC> \
    --out docs/measurements/gps_open_sky
```

The analyzer writes `<run>_summary.json` and `<run>_tables.md`, checks that no
coordinate columns were written, and reports the distinct boot IDs. If the Pi
is left on after trial 3, the app is stopped but SSH works, so this can be done
without another power cycle.

### Re-arm / stop

```bash
# cancel a campaign that has not completed
./fatigue-env/bin/python3 tools/run_gps_trials.py --disarm

# optional: resume the normal app after an aborted/error trial
sudo systemctl start transitguard
```

The unit stays enabled; with no `BOOT_STATE.json` its condition fails, so it
does nothing and `transitguard` starts normally.

## Artifacts

```
logs/gps_trials/
  BOOT_STATE.json                 armed state (removed after trial 3)
  BOOT_STATE.done.json            final state after trial 3
  index.jsonl                     one line per completed trial
  run_<UTC>/
    manifest.json                 receiver, app hash, settings, arm-time power state
    run_summary.json              campaign state after each completed trial
    trial_1/{samples.csv,events.jsonl,summary.json}
    trial_2/...
    trial_3/...
```

`samples.csv` has one row per second: `elapsed_s`, `fix_valid`, `fix_quality`,
`sats_used`, `sats_in_view`, `best_snr`, `hdop`, `sec_since_last_fix`. It has no
latitude/longitude/altitude columns. `summary.json` has TTFF, fix fractions,
cold-start basis, boot ID, uptime, temperatures, `get_throttled` and the module
identity (`UBX-MON-VER`).

## Non-claims

- **No position accuracy.** There is no surveyed reference; only fix validity,
  TTFF, satellite counts and HDOP are reported.
- **No moving-vehicle claim.** The Pi is stationary in open sky, with the app
  stopped. This does not demonstrate in-vehicle GPS performance.
- **No long-term or fleet claim.** Three short trials at one location/time.
- **No clean-power claim.** `get_throttled` is recorded; the Pi has shown
  `0x50005` (under-voltage + throttling) on earlier runs. A non-zero value does
  not invalidate fix availability, but it must be reported.
- **No verified receiver cold start.** The UBX cold-start command was not
  acknowledged by this receiver/clone, and the backup domain may retain
  almanac/ephemeris across the Pi power cycle. These are **power-cycle starts**;
  the analyzer records that the UBX reset was not observed and reports a
  power-on TTFF where possible. A fix already present at trial start is reported
  only as an upper bound.
