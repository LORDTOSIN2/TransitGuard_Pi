# TransitGuard — Raspberry Pi 4 Driver Fatigue & Vehicle Monitoring System

A low-power IoT vehicle monitoring system built on a Raspberry Pi 4, combining
real-time driver fatigue detection, GPS tracking, alcohol sensing, and
dual-path telemetry (Wi-Fi/cellular primary, LoRa fallback) with local
audible/visual alerts and a live browser dashboard.

## Features

- **Driver fatigue detection** — MediaPipe Face Landmarker (Tasks API), computing
  EAR, MAR, PERCLOS, and head pose (pitch/yaw/roll) in real time
- **Pose-gated EAR/MAR** — eye/mouth metrics are only trusted when the face is
  reasonably frontal, preventing false alerts from head-angle distortion
- **Alcohol sensing** — MQ-3 gas sensor via an ADS1115 ADC, with a calibration
  workflow for Rs/Ro baseline
- **GPS tracking** — NEO-6M module (NMEA via UART), with automatic fallback to
  the last known good fix (with staleness tracking) when signal is lost
- **Dual-path telemetry** — primary upload to a Supabase Edge Function over
  Wi-Fi/cellular, with automatic LoRa fallback when the primary path is down
  (plus safety-critical alerts always relayed over LoRa for redundancy)
- **Offline resilience** — SQLite store-and-forward buffer for telemetry sent
  while offline, flushed automatically once connectivity returns
- **Local driver-facing alerts** — 16x2 LCD (4-bit mode) + buzzer
- **Live browser dashboard** — video feed + real-time metrics, served over the
  LAN, no monitor needed on the Pi itself
- **Performance-tuned** — threaded camera capture and non-blocking
  network/radio/actuator I/O so slow Wi-Fi or LoRa never stalls the camera loop

## Hardware

| Component | Interface |
|---|---|
| Raspberry Pi 4 | — |
| USB/Pi camera | USB or CSI |
| NEO-6M GPS module | UART0 (`/dev/serial0`) |
| MQ-3 alcohol sensor | Analog, via ADS1115 ADC (I2C) |
| ATK-LORA-01 LoRa module | UART2 (`/dev/ttyAMA2`), GPIO0/GPIO1 |
| 16x2 LCD (HD44780) | 4-bit mode: RS, E, D4–D7 |
| Buzzer | GPIO18 |

### Wiring

```
NEO-6M GPS   -> Pi UART0 (/dev/serial0): VCC, GND, TX->GPIO15, RX->GPIO14
MQ-3         -> ADS1115 A0 -> Pi I2C (GPIO2=SDA, GPIO3=SCL)
ATK-LORA-01  -> Pi UART2 (/dev/ttyAMA2): TXD->GPIO1, RXD->GPIO0
               M0/M1 -> GPIO23/GPIO24, held LOW (normal transmission mode)
               Note: GPIO0/1 double as the HAT ID EEPROM pins — fine for a
               breadboard build, avoid this UART if adding a HAT with an ID EEPROM
16x2 LCD     -> RS=GPIO25, E=GPIO8, D4=GPIO7, D5=GPIO12, D6=GPIO16, D7=GPIO20
               RW->GND, contrast via a 10k pot on V0
Buzzer       -> GPIO18, GND
```

Enable the extra UART for LoRa in `/boot/firmware/config.txt`:
```
dtoverlay=uart2
```

## Setup

```bash
git clone <this-repo-url>
cd transitguard

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Download the MediaPipe face landmark model
wget -O face_landmarker.task https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task

# Configure your environment
cp .env.example .env
nano .env   # fill in your real Supabase URL/token, etc.
```

**MediaPipe version note:** newer MediaPipe releases removed the legacy
`mp.solutions` API in favor of the Tasks API used here. If you hit
`AttributeError: module 'mediapipe' has no attribute 'solutions'`, you're on
an incompatible version — this project targets the Tasks API
(`mediapipe.tasks.python.vision.FaceLandmarker`), which should work with
current releases out of the box.

## Calibration

EAR/MAR thresholds vary by individual eye/mouth shape. Before relying on
alerts, calibrate your own baseline:

```bash
python3 tools/calibrate_ear.py
```

Face the camera normally for 10 seconds; it prints a suggested
`EAR_THRESHOLD` to set in `vehicle_monitor.py`.

For the MQ-3 sensor, run a clean-air calibration first (see
`tools/mq3_reader.py --help`-style usage in the file header) to determine
your sensor's `R0` baseline before trusting alcohol readings.

## Running

```bash
# Interactive, with live preview window
python3 vehicle_monitor.py

# Diagnostic: send one test packet to confirm auth/connectivity
python3 vehicle_monitor.py --once

# Check the offline buffer
python3 vehicle_monitor.py --status
python3 vehicle_monitor.py --flush
```

Once running, the live dashboard (video feed + metrics) is available at:
```
http://<pi-ip-address>:8080
```

## Auto-run on boot (systemd)

```bash
sudo cp systemd/transitguard.service /etc/systemd/system/
sudo nano /etc/systemd/system/transitguard.service   # adjust paths/user
sudo cp .env /etc/transitguard.env
sudo chown root:pi /etc/transitguard.env
sudo chmod 640 /etc/transitguard.env

sudo systemctl daemon-reload
sudo systemctl enable transitguard.service
sudo systemctl start transitguard.service
sudo systemctl status transitguard.service
```

Set `ENABLE_PREVIEW=false` in your env file for headless auto-run — there's
no display available under systemd, and the live dashboard is the intended
way to view the feed remotely instead.

## Standalone diagnostic tools (`tools/`)

- `calibrate_ear.py` — measures your personal EAR baseline
- `gps_reader.py` — standalone NEO-6M NMEA reader/parser
- `mq3_reader.py` — standalone MQ-3 calibration and live reading
- `lora_at_config.py` — AT-command configuration/testing for the ATK-LORA-01

## Known gaps vs. original project objectives

This implementation currently diverges from the original thesis objectives
in two ways, documented here for transparency:

- **Primary transport is Wi-Fi/cellular HTTPS (to Supabase), not literal
  GSM/GPRS hardware.** LoRa fallback logic is fully implemented and tested;
  an actual GSM/GPRS module (e.g. SIM800L) has not yet been integrated.
- **Dashboard is Supabase Realtime + a custom Flask live-view, not MQTT.**
  No MQTT broker is used in this implementation.

## License

Academic/thesis project. Add a license of your choice if open-sourcing.
