# Camera-pipeline availability smoke test — 2026-10-22

**Status: complete.** One short, stationary, consented test with a face present,
absent and present again. Metrics only — **no video, images or frames were
retained**.

**Result: the pipeline produced valid face metrics for 96.2–99.1 % of seconds
while a face was in view, and reported no face for 96.5 % of the face-absent
phase.** The camera loop ran at **~4.4 FPS with a face present** and ~19 FPS
without one.

> This is an **availability** check, not a detection-accuracy test. It does not
> show that EAR/MAR/PERCLOS or the classifier are correct — only that the live
> camera path produces timestamped metrics when a face is present and correctly
> reports absence when the face leaves. The low camera availability measured
> during the three vehicle sessions (0 %, 1.1 %, 0 %) is **unchanged** and still
> stands as measured in that environment.

## Why this was run

The post-mitigation campaign recorded camera metrics in only 0–1.1 % of samples.
Before the paper can claim live camera-signal operation at all, the pipeline
needs to be shown working under a controlled condition where a face is definitely
present. This test supplies that narrow evidence and keeps the campaign's low
figures explicit.

## Conditions

| Item | Value |
|---|---|
| Date/time | 2026-10-22, 11:00:35–11:05:45 UTC (12:00–12:05 BST) |
| Device | BUS-101 (Raspberry Pi 4), stationary on a desk |
| Network | `SFM MBB - CONF` (unchanged; test does not need the uplink) |
| App build | `vehicle_monitor.py` SHA-256 `1ecd75dcb646…`, git HEAD `910de89` + this gated hook |
| Model | `face_landmarker.task` SHA-256 `64184e229b26…` |
| Camera | `/dev/video0` (USB), 640×480 |
| Clock | NTP synchronised (`NTPSynchronized=yes`) |
| Power/thermal | `get_throttled 0x50005` throughout; 63.8 °C before, 70.1 °C peak, 68.2 °C after |
| Disk | 578 MB free before; metrics file 70 KB |
| Consent | operator present and following the announced phases |

The logging hook is gated by `CAMERA_METRICS_LOG_PATH` (off by default). It adds
one JSON line per second with `face_present`, `frontal`, `ear`, `mar`,
`perclos`, `pitch`, `yaw`, `roll`, `driver_state`, `frames_processed` and `fps`.
No frame data is written by the hook; the dashboard stream is in-memory only.

## Method

1. Preflight: 63.8 °C, 578 MB free, no blackhole routes, service active, camera
   device present.
2. Harness installed a temporary drop-in setting
   `CAMERA_METRICS_LOG_PATH=logs/camera_smoke/<UTC>/metrics.jsonl`, restarted the
   service and waited for a fresh heartbeat plus five log lines.
3. Three announced phases (UTC): **present 11:00:40–11:02:40**, **absent
   11:02:40–11:03:40**, **present 11:03:40–11:05:40**. The operator sat facing
   the camera, stepped out of frame, then returned. No fatigue was induced:
   eyes were open and posture normal throughout.
4. Teardown at 11:06:02: drop-in removed, service restarted, and
   `CAMERA_METRICS_LOG_PATH` verified **absent** from `/proc/<pid>/environ`.

## Results (1 Hz samples)

| Phase | Samples | Face present | Frontal | EAR avail. | MAR avail. | PERCLOS avail. | Median EAR | Median MAR | Median PERCLOS | Median FPS | Max gap | Classifier output |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| present 1 | 107 | **99.1 %** | 99.1 % | 99.1 % | 99.1 % | 99.1 % | 0.301 | 0.354 | 0.032 | 4.43 | 1.36 s | NORMAL 104, DROWSY 3 |
| absent | 57 | **3.5 %** | 3.5 % | 3.5 % | 3.5 % | 3.5 % | 0.288 | 0.328 | 0.020 | 19.21 | 1.24 s | NORMAL 57 |
| present 2 | 104 | **96.2 %** | 96.2 % | 96.2 % | 96.2 % | 96.2 % | 0.323 | 0.368 | 0.040 | 4.34 | 1.35 s | NORMAL 91, DROWSY 13 |

* 279 metrics lines, no gaps longer than 1.4 s, so the 1 Hz logger itself did not
  stall.
* The 3.5 % "face present" in the absent phase is **two samples at 11:02:41–42**,
  i.e. the transition while the operator was leaving. No sustained false
  presence.
* Announced labels match observed face presence: **true**.
* The camera path never failed to produce a frame; the limiting factor is
  **throughput**, not availability.

### Frame rate is the real constraint

| Condition | Samples | Median FPS | Min | Max |
|---|---|---|---|---|
| Face present | 218 | **4.39** | 0.97 | 4.82 |
| No face | 61 | **19.14** | 4.80 | 19.93 |

With a face tracked, the Pi 4 runs the MediaPipe landmark + pose + EAR/MAR path
at about **4.4 FPS** — roughly one frame every 230 ms. That is enough for the
10 s telemetry cadence and for second-by-second availability, but it is a
material limit on blink/closure timing. The 60 s PERCLOS window therefore
contains ~260 frames, not ~1 200. Do not describe this as a 20–30 FPS pipeline.

### Classifier output, not accuracy

The classifier emitted `DROWSY` for 17 of 279 samples (16.1 s), all while a face
was present and the operator's eyes were open. The records show this was driven
by **PERCLOS**, not sustained eye closure: e.g. 11:03:54 has `ear=0.170` (one
blink) and `perclos=0.189`; the state stays `DROWSY` while PERCLOS remains
≥ 0.15 and returns to `NORMAL` as it decays. In present 1 the first three samples
also show PERCLOS ≈ 0.16–0.21 at session start.

Two consequences worth stating in the paper:

1. **PERCLOS carries across the face-absent gap.** The 60 s window is not cleared
   when the face is lost, so the first seconds after reacquisition can inherit
   pre-gap closed-eye samples. In present 2 the elevated PERCLOS at 11:03:54–56
   follows the 60 s absence; it decays normally afterwards.
2. **A blink can trip the DROWSY threshold** at this frame rate (`EAR < 0.21`
   for one sample, or PERCLOS ≥ 0.15). This is a classifier-output observation,
   not a false-positive rate: there is no independent reference for the
   operator's eye state.

## Non-claims

* **No accuracy claim.** One consenting adult, one camera, one indoor lighting
  condition, five minutes. No reference eye/mouth measurement, no labelled
  dataset, no video retained (by design), so nothing here estimates sensitivity
  or specificity.
* **No fatigue was induced.** Eyes open, normal posture; `DROWSY` is the
  classifier's output, not an instructed state.
* **Face presence is the pipeline's own output.** Phase compliance is
  corroborated by the sharp transition and the operator's announced actions, but
  with no independent camera record there is no second source for "a face was
  there".
* **Not a fix for the campaign figures.** The vehicle sessions' 0–1.1 % camera
  availability stands as measured. This test does not explain it (face not in
  view, lighting, mounting or distance could all contribute); it only shows the
  pipeline can operate when a face is in view.
* **Under-voltage still present** (`0x50005`), so timing figures are not
  clean-power figures.

## Artifacts

| File | Contents |
|---|---|
| `raw/20261022T110016Z/manifest.json` | app/model hashes, camera devices, clock sync, config keys (values redacted) |
| `raw/20261022T110016Z/preflight.json` | temperature, throttle, disk, service, camera devices |
| `raw/20261022T110016Z/phases.json` | exact UTC/local boundaries of the three phases |
| `raw/20261022T110016Z/metrics.jsonl` | 279 metrics-only JSON lines (1 Hz) |
| `raw/20261022T110016Z/samples.csv` | 10 s poll: temperature, throttle, app face state |
| `raw/20261022T110016Z/postflight.json`, `postflight_teardown.json` | end readings + teardown verification |
| `raw/20261022T110016Z/summary.json` | harness summary |
| `raw/20261022T110016Z/harness.log` | full harness output |
| `20261022T110016Z_summary.json` | independent reconciliation (this analysis) |
| `20261022T110016Z_tables.md` | generated table above |

No image or video file exists in the run folder (checked by the analyzer).

## Reproduce

```bash
# on the Pi (drop-in is managed by the harness; the service is restarted twice)
python3 tools/run_camera_smoke.py --present-sec 120 --absent-sec 60

# locally
python tools/analyze_camera_smoke.py <run_dir> --out docs/measurements/camera_smoke
```
