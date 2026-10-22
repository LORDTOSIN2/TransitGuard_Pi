# GPS open-sky availability — method and artifacts

**Question:** after a cold start, can the NEO-6M acquire a valid GPS fix and hold
it while the receiver is stationary with a clear view of the sky?

**Claim scope:** fix availability and time-to-first-fix (TTFF) only. There is no
surveyed reference position here, so this study makes **no position-accuracy
claim**. It is also not a moving-vehicle, fleet, or long-term reliability test.

> **Status:** the harness and boot unit are implemented; the three power-on
> trials are pending. Results will be added below after the Pi is powered on in
> open sky.

## Why a power-cycle per trial

The receiver has never achieved a fix in the vehicle sessions (0 % in all three
post-mitigation sessions; the module streamed NMEA with `fix_quality 0`,
0 satellites used, and 0–2 in view). The likely cause is antenna/sky view, not
the Pi UART. This test removes the vehicle and workload and gives the receiver
the best available sky view.

Each trial is one **full Pi power cycle**, so the receiver is genuinely
power-cycled too. The harness also sends a receiver-level cold-start command
(`UBX-CFG-RST`, `navBbrMask=0xFFFF`, `resetMode=0x02`) at the start of the trial.
The trial summary records both bases:

- `pi_uptime_at_start_s` and a distinct `boot_id` per trial (power-cycle
  evidence), and
- the NMEA gap after the UBX reset (whether the software reset was observed).

This means the run does not depend on the UBX reset working: the power cycle is
the primary cold start, and the reset is a secondary control.

## Trial definition

| Item | Setting |
|---|---|
| Placement | stationary, antenna in open sky, no vehicle roof |
| Duration cap | **180 s** per trial (3 min) |
| Early stop | after a valid GGA fix has been held for **30 s** |
| Cold start | full Pi power cycle + `UBX-CFG-RST` at trial start |
| Sampling | 1 Hz |
| Trials | 3 power-ons (target 3) |
| App state | `transitguard` stopped during the trial (serial is exclusive); LCD shows the result |
| Data | metrics only — **no coordinates stored** |

TTFF is measured from the reset command to the first GGA with `gps_qual > 0`.
Fix-valid fraction is reported both over the whole trial window (which includes
the cold-start interval, so it is not a steady-state availability estimate) and
over the post-fix window. Satellite counts, best C/N0, HDOP, fix losses and the
age of the last fix are recorded for context only.

## Workflow

### One-time deploy and arm (over SSH, while the Pi is on)

```bash
scp tools/run_gps_trials.py tools/analyze_gps_trials.py \
    john@10.80.3.194:/home/john/fatigue/tools/

ssh john@10.80.3.194
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
scp -r john@10.80.3.194:/home/john/fatigue/logs/gps_trials/run_<UTC> \
    docs/measurements/gps_open_sky/raw/
scp john@10.80.3.194:/home/john/fatigue/logs/gps_trials/BOOT_STATE.done.json \
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
- The receiver-level cold start is a software command; the separate Pi power
  cycle is the stronger cold-start evidence. Both are recorded.
