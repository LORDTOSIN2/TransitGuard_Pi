# Dashboard test — local + remote

Iteration 2 of the dashboard test plan. Item 2 (the run-1 remote records) was already
complete; this run covers the remaining items: the Pi's local routes, the `/data`
heartbeat, JSON↔page mismatches, retained redacted screenshots + database evidence, and
browser/device/refresh/stale-field notes.

**Date:** 2026-10-16, ~09:04–09:08 UTC (10:04–10:08 WAT)
**Result:** both dashboards functional; **four defects** found (2 local-UI, 2 remote/data-fidelity). **F1, F2 and F7 are fixed** (`01488b9`, `236469a`) and the alcohol thresholds were retuned (`236469a`); **F3–F5 remain open**.

---

## 1. Environment

| Item | Value |
|---|---|
| Client | `Administrator@XtremeLite-PC`, Windows 10 x64 — on the `SFM MBB - CONF` network (same L2 as the Pi) |
| Browser | OpenCode `2.0.29` · Chromium `152.0.7977.130` · Electron `44.4.5` |
| Screen | 1366×768, devicePixelRatio 1 |
| Viewports used | 1000×700 and 800×574 (window-dependent) |
| Client timezone | `Africa/Lagos` (WAT, UTC+1) |
| Pi | Debian 12 (bookworm) aarch64, `10.80.3.194`, service `transitguard`; **booted ~11 min before the test** (fresh session, `/proc/uptime` 670 s) |
| Telemetry state | `NORMAL`, `buffer_count` 0, `alert_coalesced` 21, RSSI −68 dBm, `BUS-101` |

> Two tabs of each dashboard were already open from the run-1 session.

---

## 2. Local dashboard (Pi Flask, `http://10.80.3.194:8080`)

### 2.1 Route checks

| Route | Status | Time | Size | Content-Type |
|---|---|---|---|---|
| `/` | 200 | **12.4 s** first (cold), then 0.69 / 0.29 / **0.06 s** | 23 674 B | `text/html; charset=utf-8` |
| `/data` | 200 | 23–187 ms | 489 B | `application/json` |
| `/wifi/status` | 200 | 254 ms | 155 B | `application/json` |
| `/video_feed` | 200 | streams (capped at 4 s) | — | `multipart/x-mixed-replace; boundary=frame` |

Server is `Werkzeug/3.1.8`, started with `threaded=True` — so the MJPEG stream does **not**
block the other routes. The 12.4 s `/` was a one-off cold request; three immediate
repeats were 0.69 / 0.29 / 0.06 s.

`/wifi/status` body:
```json
{"current_ip":"10.80.3.194","current_ssid":"SFM MBB - CONF","enabled":true,
 "home_ssid":"Redmi 14C","next_ssid":"Redmi 14C","target_ssid":"SFM MBB - CONF"}
```
So the switch button correctly offers **Redmi 14C** while on SFM.

### 2.2 `/data.updated_at` heartbeat

The page polls `/data` every **1000 ms** and `/wifi/status` every **5000 ms** (verified in
the served JS), and `updated_at` is rendered verbatim.

`updated_at` advanced **10:04:11 → 10:04:30** across a 19.01 s wall window (+19 s), i.e. it
tracks real time and is a valid main-loop heartbeat.

Two timestamp caveats:
* `updated_at` is formatted in **Pi local time** (WAT/UTC+1) with no zone/date — the client
  UTC clock read ~1 h behind it.
* The Pi clock ran **~7 s ahead** of this client's local clock (stable offset 6.7–6.9 s
  across all 10 observations).

### 2.3 Ten `/data` observations

Saved verbatim in [`data_observations.txt`](data_observations.txt).
Collected ~2 s apart; summary:

| # | client UTC | http ms | updated_at | state | sats used/view | best_snr | alcohol_ratio | buffer | loc age (s) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 09:04:04.132 | 71 | 10:04:11 | NORMAL | 0 / 0 | 28.0 | 5.19 | 0 | 10114.6 |
| 2 | 09:04:06.279 | 106 | 10:04:13 | NORMAL | 0 / 0 | 28.0 | 5.19 | 0 | 10116.7 |
| 3 | 09:04:08.387 | 67 | 10:04:15 | NORMAL | 0 / 0 | 28.0 | 5.19 | 0 | 10118.8 |
| 4 | 09:04:10.460 | 44 | 10:04:17 | NORMAL | 0 / 0 | 28.0 | 5.19 | 0 | 10120.9 |
| 5 | 09:04:12.556 | 93 | 10:04:19 | NORMAL | 0 / 0 | 28.0 | 5.22 | 0 | 10123.0 |
| 6 | 09:04:14.654 | 51 | 10:04:21 | NORMAL | 0 / 0 | 28.0 | 5.22 | 0 | 10125.1 |
| 7 | 09:04:16.708 | 61 | 10:04:23 | NORMAL | 0 / 0 | 28.0 | 5.23 | 0 | 10127.2 |
| 8 | 09:04:18.782 | 156 | 10:04:25 | NORMAL | 0 / 0 | 28.0 | 5.23 | 0 | 10129.3 |
| 9 | 09:04:20.947 | 187 | 10:04:27 | NORMAL | 0 / 0 | 28.0 | 5.24 | 0 | 10131.4 |
| 10 | 09:04:23.143 | 50 | 10:04:30 | NORMAL | 0 / 0 | 28.0 | 5.33 | 0 | 10133.5 |

`updated_at` incremented by exactly +2 s per sample (matching the ~2.1 s sampling interval)
— monotonic, no stalls. `latitude/longitude` were fixed at `9.5371415, 6.4544922` with
`using_last_known: true`, `fix_valid: false`.

### 2.4 JSON ↔ page mismatches

| # | Severity | Finding |
|---|---|---|
| **M1** | **Fixed** (`01488b9`) | **MQ-3 Ratio colour was inverted.** `/data` reports `alcohol_ratio` 5.27; the page rendered it with the **`alert` (red)** class. The JS test was `m.alcohol_ratio > 0.4` (line 537), but the app's actual rule is `ratio < 0.4` → `ALCOHOL_ALERT` (`MQ3_ALERT_RATIO_THRESHOLD`, line 516; *low* = alcohol). A **safe** reading was shown in alarm red and a real alert would have shown neutral. **Fixed:** compare with `<`, add a `MQ3_CAUTION_RATIO_THRESHOLD` (1.5) amber band, expose both thresholds in `/data`, and always reset the class. |
| **M2** | **Fixed** (`236469a`) | The camera panel was labelled **"DRIVER CAM · FHD"** while the stream is **640×480**. The label is now rendered from the true frame size published in `/data` (`frame_width`/`frame_height`), so it reads **`DRIVER CAM · 640×480`** and cannot lie. |
| **M3** | Stale display | The GPS row shows `9.53714, 6.45449 (stale 2h 50m)` rather than "no fix yet". A fix was persisted at **07:15:36 WAT** (`.gps_last_known.json`), so the page faithfully renders a **2 h 50 m old** position — prominently, in amber. |
| **M4** | **Fixed** (`236469a`) | The HUD **on the video frame** was clipped: the panel is wider than 4:3, so `object-fit: cover` cropped the top off the frame and cut the burned-in overlay. Now `object-fit: contain` (full frame, black letterbox blends into the panel) and the HUD scales with the frame and is drawn over a dark outline for readability. |
| — | OK | `ear`/`mar`/`perclos`/`pitch` are `null` in JSON and render `--` (consistent). |
| — | OK | `/data` carries no SSID/RSSI; the Network card sources it from `/wifi/status` (5 s cadence), so it updates independently of the 1 s metrics. |
| — | Note | JSON fields **not** shown on the page: `alcohol_ppm`, `alert_coalesced`, `fix_quality`, and `using_last_known` (only implied by the "stale" text). |

### 2.5 Local screenshots

* [`local_redacted.png`](local_redacted.png) — **retained redacted** capture (pre-fix build — note the old "FHD" label): camera feed masked, GPS coordinates masked, SSID/IP masked.
* The unredacted originals (`local_viewport_raw.png`, `local_fullpage_raw.png`) are **not committed** — they contain the SSID/IP and stay in the local workspace.

---

## 3. Remote dashboard (`https://transit-guard-io-t.vercel.app/`)

### 3.1 Confirmed behaviour

| Check | Result |
|---|---|
| Hydration fetch | `GET /rest/v1/telemetry?select=*&order=timestamp.desc&limit=40` → **200** (1 376 ms) |
| Realtime | `wss://hfzqfcuezuxcgaifxqok.supabase.co/realtime/v1/websocket?apikey=…&eventsPerSecond=10&vsn=2.0.0` → **101** |
| Indicator | `SUPABASE LIVE (40 pkts)` at load → **47 → 48 → 50** during the session (real-time inserts arriving) |
| Basemap | 9 CARTO tiles 200 |
| Console | only `TransitGuard ServiceWorker registered`; **no errors** after a clean reload |

This reproduces the run-1 record (528/200/101/…/47) for a second session, so the remote
dashboard is functional **when the bundle loads**.

### 3.2 Deployment-skew incident (HTTP 404)

On one reload the browser served a **cached `index.html`** referencing
`assets/index-Ba72ErGV.js` → **HTTP 404**, so the app never booted (blank; no hydration, no
WebSocket, no indicator). The live `index.html` at that moment (`Last-Modified`
**09:06:30 GMT**) referenced `assets/index-Li_7Z3yw.js` (200, 732 KB). This is classic
Vercel hashed-asset skew during a rollout — transient, and it self-heals on the next
reload. Worth noting because a user hitting the site mid-deploy sees a **dead dashboard**.

### 3.3 The displayed fleet is a simulator

`App.tsx` calls `simulationEngine.start()` on mount — a **5-bus mock fleet**
(NG-MN-421…425, drivers "Ibrahim Danladi", "Musa Garba", …). Real Supabase rows are fed
**into** the simulator via `ingestLiveTelemetry()`:

* Identity fields (plate, driver, route) stay **mock**.
* Live fields update, but the merge uses `payload.x ?? bus.x`, so **fields the Pi reports
  as `null` silently keep the simulator's fabricated value**. BUS-101's recorded
  `EAR: 0.295 / PERCLOS: 4.2%` are therefore **simulator values**, not the Pi's (which are
  `null` — no face detected).
* `liveTelemetryPackets` is set to the **hydration length (40 = the fetch limit)** then
  incremented per realtime insert. So `SUPABASE LIVE (N pkts)` is **not** a count of
  distinct packets; at load it equals `min(40, rows in table)`.

Consequence: the remote dashboard is a **demo/simulation** that overlays one real device.
It must not be read as a faithful mirror of the Pi.

### 3.4 Remote screenshots

* [`remote_redacted.png`](remote_redacted.png) — **retained redacted** capture: the map is blurred, so no place names or coordinates are visible.
* The unredacted originals (`remote_viewport_raw.png`, `remote_fullpage_raw.png`) are **not committed**; the full page reveals real place names (**Minna / Shaluke / Chanchaga**) and `FUT MINNA IOT-CORE`.

---

## 4. Database evidence

Server-side, via the public REST endpoint (publishable key):

| Query | Result |
|---|---|
| Total rows | **9 861** |
| Rows created in the last 15 min | **79** (≈10 s cadence; ~90 expected) |
| Distinct `device_id` | **`BUS-101`** only |
| Latest row | `created_at` **09:07:40Z**, device `timestamp` **09:07:36Z** (≈3.7 s earlier) |
| Latest row fields | `driver_state` NORMAL · `wifi_rssi` −68 · `transport_mode` WIFI · `lcd_message` "Status: Normal \| Drive safe" |

This corroborates the remote dashboard's `SUPABASE LIVE (N pkts)` and the local
`/data.updated_at`, and re-confirms a single reporting device.

---

## 5. Findings & recommendations

| ID | Where | Finding | Recommendation |
|---|---|---|---|
| **F1** | Local | MQ-3 ratio colour inverted (`> 0.4` vs the app's `< 0.4`) | **Fixed in `01488b9`** — compare with `<`, added a 1.5 amber caution band, thresholds now come from `/data`, class reset on every refresh. |
| **F2** | Local | "FHD" label vs 640×480 stream | **Fixed in `236469a`** — label rendered from the frame size reported in `/data`. |
| **F3** | Remote | Simulator presented as live; real `null`s inherit fabricated values | Show an explicit "no face / simulated" state, or stop the sim once live telemetry connects. |
| **F4** | Remote | "SUPABASE LIVE (N pkts)" = fetch limit + increments | Separate "hydrated rows" from "live inserts"; label the limit. |
| **F5** | Remote | Hashed-asset 404 on mid-deploy reload → blank app | Keep previous hashed assets briefly, or have the SW serve a cached shell and refresh. |
| **F6** | Local | 2 h 50 m stale last-known position shown prominently | Already flagged "stale"; consider suppressing > N minutes or showing "no current fix". |
| **F7** | Local | On-frame HUD overlay clipped | **Fixed in `236469a`** — `object-fit: contain` (no crop) plus a frame-scaled, outlined HUD. |
| **F8** | Local | `/data` jitter up to 187 ms; cold `/` 12.4 s | Watch for a blocking path; not reproducible (threaded server). |
| **F9** | Local / app | **The alert threshold was unreachable.** Alcohol near the MQ-3 drives the ratio to ~0.88, so the old `MQ3_ALERT_RATIO_THRESHOLD = 0.4` could never fire. | **Retuned in `236469a` on the operator's instruction:** alert `< 2.5`, amber caution `2.6–4`, clean `≥ 4` (`MQ3_ALERT_RATIO_THRESHOLD = 2.5`, `MQ3_CAUTION_RATIO_THRESHOLD = 4.0`). This changes `ALCOHOL_ALERT`, the buzzer and the LoRa alert too. A clean-vs-alcohol calibration sweep is still recommended to confirm the numbers. |

**Verdict:** local dashboard is reachable, threaded, and its heartbeat is sound; the remote
dashboard hydrates and streams correctly when its bundle loads. **F1, F2, F7 and F9 are
fixed** (MQ-3 colour correct and thresholds retuned; camera label truthful; on-frame HUD no
longer cropped). The remaining high-value issue is **F3** (fabricated biometrics shown as
live on the remote dashboard); **F5** is a transient deploy-skew 404.

---

## 6. Fixes applied (follow-up)

| Defect | Commit | What changed |
|---|---|---|
| F1 | `01488b9` | MQ-3 colour compared with `>` instead of `<`; safe readings showed red and real alerts showed neutral. Now correct, threshold-driven from `/data`, class reset each refresh (was sticky). |
| F9 | `236469a` | `MQ3_ALERT_RATIO_THRESHOLD` 0.4 → **2.5**, `MQ3_CAUTION_RATIO_THRESHOLD` 1.5 → **4.0** (alert `< 2.5`, amber `2.6–4`, clean `≥ 4`). |
| F2 | `236469a` | Hardcoded "FHD" label replaced by the real frame size (`/data` `frame_width`/`frame_height`). |
| F7 | `236469a` | `object-fit: contain` (no crop) + frame-scaled, dark-outlined HUD text. |

Verified live after deploy (`236469a`): `/data` → `frame_width 640, frame_height 480`,
`alcohol_alert_threshold 2.5`, `alcohol_caution_threshold 4.0`; the page label reads
**`DRIVER CAM · 640×480`**; the MQ-3 ratio (~5.5) renders neutral; the burned-in HUD
(`No face detected`, `State: NORMAL`, GPS, `MQ-3 ratio`) is fully inside the frame.

> Capture note: the browser-screenshot harness rendered later captures in a light palette
> even though the page's computed theme is dark (`body` background `rgb(17,18,20)`,
> `--text-primary #e8eaed`, `.cam-panel` `rgb(0,0,0)`). The fixes above were therefore
> verified from `/data`, the DOM and computed styles rather than from a new screenshot.
