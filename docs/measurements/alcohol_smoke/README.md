# MQ-3 alcohol-vapor response — smoke test (2026-10-23)

**Question:** does the MQ-3/ADS1115 path respond to an alcohol-vapor source, and
does the running app's alert threshold fire?

**Claim scope:** a **relative vapor response** only. This is not a breathalyzer:
there is no calibrated alcohol concentration, no BAC/BrAC, and no
position/level accuracy claim. The app's `alcohol_ppm` field is an uncalibrated
curve estimate and is not used here.

## Setup

- Pi running `transitguard`; MQ-3 on ADS1115 A0; `/data` polled at 1 Hz by
  `tools/record_alcohol_test.py`.
- Sensor powered and allowed to settle for ~3 min 45 s before the first
  exposure (the operator's stated 3–5 min warm-up; shorter than the
  datasheet's 20–30 min recommendation, so this is a functional check only).
- Source: alcohol applied near the sensor by the operator; the exact alcohol,
  distance, amount and duration were not instrumented. Two response episodes
  were recorded.
- App tuning at the time: `alcohol_ratio = Rs / 6.78`,
  alert `< 2.5`, caution `2.6–4.0`, clean `≥ 4.0`. These thresholds are
  empirical, not a legal or medical limit.

## Results

Baseline (first 225 s, clean air): mean **7.88**, median **7.90**, SD **0.13**,
range **7.44–8.17**.

| Episode | Onset (UTC) | Minimum ratio | Time of minimum | Alert `< 2.5` | Below caution `< 4.0` | Recovered `≥ 4.0` |
|---|---|---|---|---|---|---|
| 1 | 06:01:42 | **0.684** | 06:01:45 | 20 s | 45 s | 06:02:28 |
| 2 | 06:05:41 | **1.324** | 06:05:44 | 8 s | 20 s | 06:06:02 |

- The first episode dropped **~91 %** below the pre-exposure baseline
  (`0.684 / 7.90`); the second dropped **~81 %** below the local baseline
  (`1.324 / 6.93`). These are relative amplitudes, not concentrations.
- `driver_state` was `ALCOHOL_ALERT` for **30 samples total** (20 s in episode 1,
  8 s in episode 2). The app entered the alert state that drives the LCD/buzzer;
  those outputs were not separately logged.
- The ratio was still **6.71** at the end of the 560 s recording, i.e. above the
  alert threshold but below the initial baseline; the sensor baseline drifted
  downward during the session.
- SoC temperature 54.5–68.2 °C; `get_throttled 0x50005` throughout (not a
  clean-power run).

## Artifacts

- [`raw/20261023T055756Z/samples.csv`](raw/20261023T055756Z/samples.csv) —
  1 Hz `alcohol_ratio`, `alcohol_ppm`, `driver_state`, thresholds, temperature,
  `get_throttled`; no raw ADC voltage.
- [`raw/20261023T055756Z/summary.json`](raw/20261023T055756Z/summary.json) —
  min/max/mean, alert first/last timestamps.
- Recorder: `tools/record_alcohol_test.py` (passive `/data` poller; no ADC access).

## Non-claims

- No calibrated alcohol concentration, BAC, BrAC or “level” is reported.
- The MQ-3 is cross-sensitive to other VOCs, humidity and temperature; the
  isopropyl source is not a breath sample.
- One sensor, one session, two manual exposures, no reference instrument.
- The app's `alcohol_ppm` is not used and should not be quoted as a
  concentration.
