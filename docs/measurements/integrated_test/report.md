# Integrated outdoor campaign report

**Test date:** 2026-10-23
**System:** TransitGuard on Raspberry Pi 4
**Scope:** Three short outdoor sessions with GPS, camera, MQ-3 and telemetry
availability sampling. This is a functional/availability record, not a
calibration or accuracy study.

## Executive summary

Two three-session campaigns were run. The first run (`outdoor1`) recorded no
fresh GPS fix in any session and therefore only had the stale last-known
location. The harness was then changed to require three consecutive, distinct
fresh GPS samples before a session can start. In the second run (`outdoor2`),
the gate waited **8 min 41 s** from the start of the pre phase, confirmed the
fix with three fresh samples, and all three sessions then reported fresh fixes
for **100% of their samples**.

The second campaign completed without aborting. It captured 1,042 samples
including the pre-fix wait and inter-session gaps. Its measured sessions
recorded a median of five satellites used and median HDOP of 1.8. A fresh GPS
fix is an availability observation only: there is no surveyed reference, so
the run does not establish position accuracy.

The Pi remained under active undervoltage/throttling (`get_throttled=0x50005`)
during the test. Peak SoC temperature was **81.3 C**, below the harness's 86 C
abort limit but high enough to merit cooling and a proper supply. The test
cannot attribute the earlier GPS failure to the power issue; antenna
placement, sky visibility, receiver state and power are possible factors.

## Run comparison

| Measure | `outdoor1` | `outdoor2` |
|---|---:|---:|
| Result | Completed; no fresh fix | Completed; GPS gate passed |
| GPS pre-session behavior | Fixed 20 s pre phase | Waited 8 min 41 s for three distinct fresh samples |
| Fresh-fix availability, session 1 | 0% | 100% |
| Fresh-fix availability, session 2 | 0% | 100% |
| Fresh-fix availability, session 3 | 0% | 100% |
| Median satellites used, sessions | 0 | 5 per session |
| Reported median HDOP, sessions | 25.5 placeholder (no fix) | 1.8 per session |
| Last-known position age at start | About 7.6 hours | Fresh fix acquired before session 1 |
| Peak SoC temperature | 71.1 C | 81.3 C |
| Undervoltage/throttle flags | `0x50005` | `0x50005` |
| Telemetry | 69 HTTP 201; 1 HTTP 502; 0 dropped | 132 HTTP 201; 5 failed attempts; 0 dropped |

The two runs were not a controlled comparison; environmental and placement
conditions were not held constant. The outcome is consistent with the fresh-fix
gate preventing a measurement session from starting before GPS is available,
but does not by itself prove why the first run failed.

## `outdoor2` session results

The boot campaign started at 09:44:06 UTC. The app and trace were ready at
09:44:27 UTC; the pre phase began with no fix. Three distinct fresh samples
confirmed readiness at 09:53:08 UTC. The three 150-second session windows
followed, separated by two 30-second gaps.

| Session | Fresh GPS samples | Satellites used (median) | HDOP (median) | Reported distance* | Face present | SoC temperature |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 150 / 150 (100%) | 5 | 1.8 | 118.5 m | 33.3% | 72.5-76.0 C |
| 2 | 150 / 150 (100%) | 5 | 1.8 | 201.4 m | 30.0% | 75.0-79.9 C |
| 3 | 150 / 150 (100%) | 5 | 1.8 | 88.0 m | 34.7% | 78.9-81.3 C |

Overall, the harness recorded 1,042 one-second samples; 50.2% had a fresh fix
when the pre-fix wait and gaps are included. The reported cumulative
coordinate-to-coordinate distance was 486.7 m over the whole run.

*\*Distance is calculated from the receiver's own coordinates. It is not
surveyed ground truth or a position-accuracy measurement.*

### Telemetry and network

- 132 telemetry messages were scheduled.
- 137 attempts were recorded: 132 received HTTP 201 responses and five
  attempts failed at the transport layer while the network was unavailable or
  timing out. All 132 scheduled unique message IDs eventually had a successful
  attempt; there were no dropped messages in the local trace.
- Session 3 included 52 samples reporting the network offline and a queue
  high-water mark of three.
- Supabase rows were **not independently reconciled** for this report. HTTP 201
  records acceptance by the ingest function, not proof of a stored database
  row.

### Other sensor and thermal observations

The camera reported a face in 18.4% of all samples overall and 30.0-34.7% in
the measured sessions. No video or images were retained, and no fatigue was
induced. These are availability observations, not fatigue-detection accuracy
or driver-condition evidence.

The MQ-3 ratio median was 5.763 overall. It is an uncalibrated relative vapor
reading, not BAC/BrAC or a validated alcohol concentration. Sensor exposure,
warm-up and calibration were not controlled by this integrated run.

The Pi's temperature rose from 48.2 C at preflight to 81.3 C maximum. It was
78.4 C at postflight. `0x50005` indicates current and previously recorded
undervoltage and throttling flags. The run remained below the harness's 86 C
abort threshold, but it should not be described as power-healthy or
unthrottled.

## Artifacts and privacy

The public, sanitized evidence copies are under
[`results/outdoor1/`](./results/outdoor1/) and
[`results/outdoor2/`](./results/outdoor2/). The per-run summary, samples,
delivery counts and harness journal are available there. The published sample
files have blank latitude/longitude fields; the exact-coordinate copies remain
only in the local pulled `raw/` folders and are not included in the repository.
Local SSIDs/IPs and raw telemetry message IDs are also omitted from the public
evidence copy.

No Supabase matching, surveyed GPS reference, independent position validation,
fatigue accuracy study, calibrated alcohol measurement, or long-term
reliability inference is included.
