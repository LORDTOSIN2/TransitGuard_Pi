"""
Integrated Vehicle Monitoring Subsystem — Supabase Telemetry Edition
------------------------------------------------------------------------
Combines:
  - Driver fatigue detection (MediaPipe Tasks API: EAR, MAR, PERCLOS, head-pose)
  - GPS location tracking (NEO-6M, NMEA parsing via pynmea2)
  - Alcohol sensing (MQ-3 via ADS1115 ADC over I2C)
  - LoRa transmission (ATK-LORA-01) — kept as a separate, independent low-rate
    transport for the offline-fallback scenario your thesis describes
  - Supabase telemetry upload (HTTPS POST to the ingest-telemetry Edge
    Function), following the exact JSON contract and field names from
    RASPBERRY_PI4_SUPABASE_INTEGRATION.docx, with an offline SQLite
    store-and-forward buffer matching that document's design.
  - 16x2 LCD (4-bit mode) + buzzer for local driver-facing alerts

Wiring:
    NEO-6M GPS   -> Pi UART0 (/dev/serial0): VCC, GND, TX->GPIO15, RX->GPIO14
    MQ-3         -> ADS1115 A0 -> Pi I2C (GPIO2=SDA, GPIO3=SCL)
    ATK-LORA-01  -> Pi UART2 (/dev/ttyAMA2), enabled via dtoverlay=uart2
                    in /boot/firmware/config.txt: TXD->GPIO1, RXD->GPIO0.
                    M0/M1 -> GPIO23/GPIO24, held LOW (normal mode).
                    Note: GPIO0/1 double as the HAT ID EEPROM pins
                    (ID_SD/ID_SC) — fine for a breadboard build, but
                    avoid this UART if you add a HAT with an ID EEPROM.
    16x2 LCD     -> RS=GPIO25, E=GPIO8, D4=GPIO7, D5=GPIO12, D6=GPIO16,
                    D7=GPIO20, RW->GND, contrast via 10k pot on V0.
    Buzzer       -> GPIO18, GND
    Camera       -> USB or Pi Camera port

Install dependencies:
    pip install opencv-python mediapipe numpy pyserial pynmea2 \
                adafruit-blinka adafruit-circuitpython-ads1x15 \
                RPi.GPIO requests psutil

Download the model file once (same folder as this script):
    wget -O face_landmarker.task https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task

Configuration:
    Set these as real environment variables, OR put them in a .env file
    in this folder, OR /etc/transitguard.env for systemd deployment
    (see the integration guide's Section 9 and Section 13, Step 4):

        SUPABASE_INGEST_URL=https://<project-ref>.supabase.co/functions/v1/ingest-telemetry
        TRANSITGUARD_DEVICE_ID=BUS-101
        TRANSITGUARD_DEVICE_TOKEN=<your real device token>
        BUFFER_DB_PATH=/home/pi/.transitguard_buffer.db
        PUBLISH_INTERVAL_SEC=10.0
        MAX_BUFFERED_PACKETS=5000

    IMPORTANT: Never commit a real TRANSITGUARD_DEVICE_TOKEN to a public
    repo or share it in documents. Rotate it if it has ever been exposed.

Run:
    python3 vehicle_monitor.py            # full sensor + upload loop
    python3 vehicle_monitor.py --once     # send one diagnostic packet and exit
    python3 vehicle_monitor.py --status   # print local offline-buffer count
    python3 vehicle_monitor.py --flush    # force-flush the offline buffer now
"""

import cv2
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
import serial
import pynmea2
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_ads1x15 import ads1x15
import RPi.GPIO as GPIO
import time
import csv
import json
import math
import os
import sqlite3
import sys
import uuid
import argparse
import threading
import queue
import re
import signal
import subprocess
import shlex
from collections import deque

import requests
from flask import Flask, Response, jsonify, render_template_string

try:
    import psutil
    HAVE_PSUTIL = True
except ImportError:
    HAVE_PSUTIL = False

try:
    from RPLCD.gpio import CharLCD
    HAVE_RPLCD = True
except ImportError:
    HAVE_RPLCD = False


# ---------------------------
# ENVIRONMENT / CONFIGURATION
# ---------------------------
def load_env_file(path):
    """Loads KEY=VALUE lines from a .env-style file into os.environ,
    without overwriting variables that are already set. Fails silently
    on permission errors — this is a convenience fallback for manual
    runs; under systemd, EnvironmentFile= already provides these vars
    directly, so this file doesn't strictly need to be readable here."""
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except PermissionError:
        print(f"Note: could not read {path} directly (permission denied) — "
              f"continuing with whatever environment variables are already set.")


load_env_file("/etc/transitguard.env")
load_env_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

MODEL_PATH = "face_landmarker.task"

EAR_THRESHOLD = 0.21
MAR_THRESHOLD = 0.6
PERCLOS_WINDOW_SEC = 60
PERCLOS_THRESHOLD = 0.15          # DROWSY threshold
PERCLOS_CRITICAL_THRESHOLD = 0.60  # CRITICAL_FATIGUE threshold
EAR_CONSEC_FRAMES = 15
HEAD_PITCH_THRESHOLD = 15          # DROWSY threshold
HEAD_PITCH_CRITICAL_THRESHOLD = 20  # CRITICAL_FATIGUE threshold

# EAR/MAR are only geometrically reliable when the face is reasonably
# frontal — beyond these angles, perspective foreshortening falsely
# lowers EAR even with eyes open. Frames outside this range don't update
# EAR/PERCLOS at all, rather than feeding in unreliable data.
FACE_YAW_LIMIT_FOR_EAR = 25    # degrees
FACE_PITCH_LIMIT_FOR_EAR = 25  # degrees

GPS_UART_PORT = "/dev/serial0"
GPS_BAUD_RATE = 9600
# File persisted across reboots so the last known GPS fix is immediately
# available on startup instead of waiting for a fresh cold-start acquisition.
GPS_LAST_KNOWN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".gps_last_known.json"
)
# RMC sentences arrive at 1 Hz (faster on some modules), but the on-disk copy
# only has to be good enough for the next boot, so it is refreshed on a timer
# rather than on every fix — while still capturing real movement promptly.
GPS_LAST_KNOWN_MIN_INTERVAL_SEC = 30.0   # periodic refresh while parked
GPS_LAST_KNOWN_MIN_MOVE_M = 50.0         # …or once the vehicle moved this far

# Some NEO-6M / clone boards ship with only RMC enabled. Without GGA the
# satellite count and fix quality are unknowable, and without GSV there is no
# satellites-in-view or SNR to diagnose a stubborn "no fix". At startup we ask
# the module for them via UBX-CFG-MSG — a harmless no-op on modules that don't
# speak UBX.
GPS_ENABLE_SENTENCES = (
    os.environ.get("GPS_ENABLE_SENTENCES", "true").strip().lower() in ("1", "true", "yes")
)

# Camera recovery: when a USB camera fails mid-stream, cap.read() keeps
# returning False. Retrying that in a tight loop used to peg a core at 100%
# CPU and silently stop all telemetry, so back off and then reopen the device.
CAMERA_RETRY_BACKOFF_SEC = 0.25        # first retry delay, doubling up to…
CAMERA_RETRY_BACKOFF_MAX_SEC = 2.0     # …this cap
CAMERA_REOPEN_AFTER_FAILURES = 30      # consecutive failures before reopening
CAMERA_REOPEN_DELAY_SEC = 1.0

MQ3_RL_VALUE = 10.0
MQ3_R0 = 6.78  # PLACEHOLDER — replace with your calibrated R0
MQ3_VCC = 5.0
MQ3_CURVE_A = 0.4
MQ3_CURVE_B = -1.5
# The MQ-3 ratio FALLS when alcohol is present (clean air reads ~5.x, a real
# alcohol response near the sensor reads ~0.9). Below ALERT -> ALCOHOL_ALERT
# (red + buzzer); ALERT..CAUTION -> amber caution on the dashboard only;
# >= CAUTION -> clean. Retuned 2026-10-16 from field observation: a real
# alcohol response sits around 0.88, so the previous 0.4 alert threshold could
# never fire. Re-check against a clean-vs-alcohol calibration sweep.
MQ3_ALERT_RATIO_THRESHOLD = 2.5
MQ3_CAUTION_RATIO_THRESHOLD = 4.0
MQ3_POLL_INTERVAL_SEC = 1

LORA_UART_PORT = "/dev/ttyAMA2"    # Pi 4 UART2 (GPIO0=TXD2, GPIO1=RXD2)
LORA_BAUD_RATE = 9600
LORA_M0_PIN = 23
LORA_M1_PIN = 24
LORA_HEARTBEAT_INTERVAL_SEC = 5

# 16x2 LCD (HD44780, 4-bit mode) configuration
LCD_RS_PIN = 25
LCD_E_PIN = 8
LCD_D4_PIN = 7
LCD_D5_PIN = 12
LCD_D6_PIN = 16
LCD_D7_PIN = 20
LCD_COLS = 16
LCD_ROWS = 2
LCD_BOOT_IP_DISPLAY_SEC = 8  # how long the IP address stays on screen at startup

# Buzzer configuration
BUZZER_PIN = 18
BUZZER_ACTIVE_HIGH = True  # set False if your buzzer module is active-low

LOG_FILE = "vehicle_monitor_log.csv"

# Supabase / TransitGuard contract configuration
SUPABASE_INGEST_URL = os.environ.get(
    "SUPABASE_INGEST_URL",
    "https://hfzqfcuezuxcgaifxqok.supabase.co/functions/v1/ingest-telemetry",
)
DEVICE_ID = os.environ.get("TRANSITGUARD_DEVICE_ID", "BUS-101")
DEVICE_TOKEN = os.environ.get("TRANSITGUARD_DEVICE_TOKEN", "")
PUBLISH_INTERVAL_SEC = float(os.environ.get("PUBLISH_INTERVAL_SEC", "10.0"))

# While a driver alert is active the main loop runs at ~20 FPS and historically
# enqueued a telemetry message on EVERY iteration. The single uploader drains
# about one message per 1.5 s, so a sustained alarm saturated the bounded queue
# and drop-oldest silently discarded ~49% of alert messages (measured live in
# run 3 — see stability_report.md). These two knobs bound the producer:
#   * ALERT_PUBLISH_MIN_INTERVAL_SEC is the minimum gap between sends while an
#     alert is active (or a state transition just occurred). Entering an alert
#     still goes out promptly — after a quiet NORMAL period the elapsed gap is
#     already >= this value — while rapid DROWSY<->CRITICAL flapping can no
#     longer storm the queue.
#   * TELEMETRY_QUEUE_MAXSIZE is a safety margin, not the primary control.
ALERT_PUBLISH_MIN_INTERVAL_SEC = float(
    os.environ.get("ALERT_PUBLISH_MIN_INTERVAL_SEC", "5.0")
)
TELEMETRY_QUEUE_MAXSIZE = int(os.environ.get("TELEMETRY_QUEUE_MAXSIZE", "50"))
BUFFER_DB_PATH = os.environ.get(
    "BUFFER_DB_PATH", os.path.expanduser("~/.transitguard_buffer.db")
)
MAX_BUFFERED_PACKETS = int(os.environ.get("MAX_BUFFERED_PACKETS", "5000"))
HTTP_TIMEOUT_SEC = 6

# Set to "false" for headless/systemd auto-run (no display available).
# Defaults to "true" so interactive terminal testing still shows the
# preview window unless you explicitly opt out.
ENABLE_PREVIEW = os.environ.get("ENABLE_PREVIEW", "true").strip().lower() in ("1", "true", "yes")

# Browser-based live dashboard (video feed + live metrics) — works over
# the network with no monitor attached to the Pi, useful both day-to-day
# and for demos/defense presentations via any laptop/projector on the LAN.
ENABLE_DASHBOARD = os.environ.get("ENABLE_DASHBOARD", "true").strip().lower() in ("1", "true", "yes")
DASHBOARD_HOST = "0.0.0.0"
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))

# --- Optional manual Wi-Fi switch (dashboard button) -----------------------
# The Pi always boots onto whichever profile has autoconnect=yes (normally
# the primary/home hotspot). These settings expose a dashboard button that
# lets an operator manually hop to a second saved SSID. The second profile is
# deliberately stored with autoconnect=no, so a power cycle always returns to
# the primary network — the switch is session-only by design.
#
# Credentials live in /etc/transitguard.env (root-only) or a local .env;
# never commit them to the repo.
WIFI_SWITCH_TARGET_SSID = os.environ.get("WIFI_SWITCH_TARGET_SSID", "").strip()
WIFI_SWITCH_TARGET_PASSWORD = os.environ.get("WIFI_SWITCH_TARGET_PASSWORD", "")
WIFI_SWITCH_HOME_SSID = os.environ.get("WIFI_SWITCH_HOME_SSID", "").strip()
WIFI_SWITCH_ENABLED = (
    os.environ.get("WIFI_SWITCH_ENABLED", "true").strip().lower() in ("1", "true", "yes")
    and bool(WIFI_SWITCH_TARGET_SSID)
)

# --- Optional telemetry tracing (connection-stability evaluation) ----------
# When TELEMETRY_TRACE_PATH is set, append one JSON object per line for every
# scheduled / attempted / dropped telemetry event, including a monotonic-timer
# round-trip and the HTTP status code. Empty path = fully disabled (default),
# so there is no behaviour change unless you opt in for a measurement run.
TELEMETRY_TRACE_PATH = os.environ.get("TELEMETRY_TRACE_PATH", "").strip()
_trace_lock = threading.Lock()


def trace_event(**fields):
    """Append one JSON line to TELEMETRY_TRACE_PATH. No-op when tracing is off."""
    if not TELEMETRY_TRACE_PATH:
        return
    fields.setdefault("wall_utc", time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()))
    fields.setdefault("wall_epoch", round(time.time(), 3))
    try:
        line = json.dumps(fields, separators=(",", ":"))
        with _trace_lock:
            with open(TELEMETRY_TRACE_PATH, "a") as fh:
                fh.write(line + "\n")
    except Exception:
        pass

# --- Test-only scripted driver-state input (evaluation harness) ------------
# Exists so an upload-path measurement can exercise alert / rapid-transition
# load WITHOUT performing fatigue actions in front of a real driver. When
# ENABLE_TEST_STATE_INPUT is true and TEST_STATE_INPUT_PATH names a file, the
# main loop overrides the classified state with the file's contents.
#
#   echo CRITICAL_FATIGUE > /home/john/fatigue/logs/test_state.txt
#
# Allowed values: NORMAL, DROWSY, CRITICAL_FATIGUE, ALCOHOL_ALERT. An empty or
# unrecognised file leaves normal detection untouched. Every packet produced
# while an override is active carries `testInjection`, so injected data can
# never be mistaken for a measured detection. Off by default — remove the two
# variables from the environment after the run.
ENABLE_TEST_STATE_INPUT = (
    os.environ.get("ENABLE_TEST_STATE_INPUT", "").strip().lower() in ("1", "true", "yes")
)
TEST_STATE_INPUT_PATH = os.environ.get("TEST_STATE_INPUT_PATH", "").strip()
TEST_STATE_CHOICES = ("NORMAL", "DROWSY", "CRITICAL_FATIGUE", "ALCOHOL_ALERT")
_test_state_cache = {"mtime": None, "value": None}

if ENABLE_TEST_STATE_INPUT and not TEST_STATE_INPUT_PATH:
    print("WARNING: ENABLE_TEST_STATE_INPUT is set but TEST_STATE_INPUT_PATH "
          "is empty; scripted state input stays disabled.")


def read_test_state():
    """Return the scripted driver state, or None when not configured.

    Re-reads only when the file's mtime changes, so calling this once per
    camera frame (~20 FPS) costs a stat() at most."""
    if not (ENABLE_TEST_STATE_INPUT and TEST_STATE_INPUT_PATH):
        return None
    try:
        mtime = os.path.getmtime(TEST_STATE_INPUT_PATH)
    except OSError:
        _test_state_cache["mtime"] = None
        _test_state_cache["value"] = None
        return None
    if mtime != _test_state_cache["mtime"]:
        try:
            with open(TEST_STATE_INPUT_PATH, "r", encoding="utf-8") as fh:
                value = fh.read().strip().upper()
        except OSError:
            value = ""
        _test_state_cache["mtime"] = mtime
        _test_state_cache["value"] = value if value in TEST_STATE_CHOICES else None
        if _test_state_cache["value"] is None:
            print(f"Test state input: ignoring unrecognised value {value!r}")
    return _test_state_cache["value"]


if not DEVICE_TOKEN:
    print("WARNING: TRANSITGUARD_DEVICE_TOKEN is not set. Every upload will "
          "fail with 401 Unauthorized until you set it (see the integration "
          "guide, Section 9).")

LEFT_EYE_CORNERS = (362, 263)
LEFT_EYE_VERTICAL_PAIRS = [(385, 380), (386, 374), (387, 373)]

RIGHT_EYE_CORNERS = (33, 133)
RIGHT_EYE_VERTICAL_PAIRS = [(160, 144), (159, 145), (158, 153)]

MOUTH_CORNERS = (61, 291)
MOUTH_VERTICAL_PAIRS = [(39, 181), (0, 17), (84, 314)]

# Flat lists of every landmark index used, purely for the on-screen dot overlay
LEFT_EYE = [LEFT_EYE_CORNERS[0], LEFT_EYE_CORNERS[1]] + [i for pair in LEFT_EYE_VERTICAL_PAIRS for i in pair]
RIGHT_EYE = [RIGHT_EYE_CORNERS[0], RIGHT_EYE_CORNERS[1]] + [i for pair in RIGHT_EYE_VERTICAL_PAIRS for i in pair]
MOUTH = [MOUTH_CORNERS[0], MOUTH_CORNERS[1]] + [i for pair in MOUTH_VERTICAL_PAIRS for i in pair]

FACE_3D_MODEL = np.array([
    (0.0, 0.0, 0.0),
    (0.0, -63.6, -12.5),
    (-43.3, 32.7, -26.0),
    (43.3, 32.7, -26.0),
    (-28.9, -28.9, -24.1),
    (28.9, -28.9, -24.1)
], dtype=np.float64)

FACE_2D_INDICES = [1, 152, 33, 263, 61, 291]


# ---------------------------
# SUPABASE OFFLINE BUFFER (SQLite store-and-forward)
# ---------------------------
def init_buffer_db():
    conn = sqlite3.connect(BUFFER_DB_PATH)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_packets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def buffer_count():
    conn = sqlite3.connect(BUFFER_DB_PATH)
    count = conn.execute("SELECT COUNT(*) FROM pending_packets").fetchone()[0]
    conn.close()
    return count


def enqueue_packet(payload):
    conn = sqlite3.connect(BUFFER_DB_PATH)
    conn.execute(
        "INSERT INTO pending_packets (message_id, payload, created_at) VALUES (?, ?, ?)",
        (payload.get("messageId"), json.dumps(payload), time.time()),
    )
    conn.commit()

    # Disk exhaustion protection: prune oldest 50 if over the cap
    count = conn.execute("SELECT COUNT(*) FROM pending_packets").fetchone()[0]
    if count > MAX_BUFFERED_PACKETS:
        conn.execute("""
            DELETE FROM pending_packets WHERE id IN (
                SELECT id FROM pending_packets ORDER BY id ASC LIMIT 50
            )
        """)
        conn.commit()
    conn.close()


def flush_buffer(max_per_cycle=15):
    """Attempts to send the oldest buffered packets. Stops at the first
    failure (assumes still offline) so we don't hammer a dead connection."""
    conn = sqlite3.connect(BUFFER_DB_PATH)
    rows = conn.execute(
        "SELECT id, payload FROM pending_packets ORDER BY id ASC LIMIT ?",
        (max_per_cycle,),
    ).fetchall()

    sent = 0
    for row_id, payload_json in rows:
        payload = json.loads(payload_json)
        if _post_telemetry(payload, source="flush"):
            conn.execute("DELETE FROM pending_packets WHERE id = ?", (row_id,))
            conn.commit()
            sent += 1
        else:
            break  # still offline, stop trying for this cycle

    conn.close()
    return sent


# ---------------------------
# SUPABASE UPLOAD
# ---------------------------
def _post_telemetry(payload, source="live"):
    """Raw HTTPS POST to the ingest-telemetry Edge Function. Returns True
    on HTTP 201, False on any failure (network, timeout, 4xx, 5xx).

    When tracing is enabled, records a monotonic-timer round-trip, the HTTP
    status code (or transport error) and the server's Date header, so that
    HTTP RTT and clock skew against Supabase can both be reported."""
    headers = {
        "Content-Type": "application/json",
        "x-device-token": DEVICE_TOKEN,
        "Authorization": f"Bearer {DEVICE_TOKEN}",
    }
    t_start = time.monotonic()
    status = None
    server_date = None
    error = None
    try:
        resp = requests.post(
            SUPABASE_INGEST_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SEC
        )
        status = resp.status_code
        server_date = resp.headers.get("Date")
        ok = (status == 201)
        if not ok:
            print(f"Supabase upload rejected: HTTP {status} - {resp.text[:200]}")
    except requests.RequestException as e:
        error = f"{type(e).__name__}: {e}"
        ok = False
        print(f"Supabase upload failed (network): {e}")
    t_end = time.monotonic()

    trace_event(
        ev="attempt",
        messageId=payload.get("messageId"),
        device_ts_ms=payload.get("timestamp"),
        source=source,
        t_mono_start=round(t_start, 6),
        t_mono_end=round(t_end, 6),
        rtt_ms=round((t_end - t_start) * 1000.0, 3),
        http_status=status,
        server_date=server_date,
        error=error,
        ok=ok,
    )
    return ok


def send_telemetry(payload, source="live"):
    """
    Tries to flush any existing backlog first (so ordering stays mostly
    correct), then sends the current packet. On failure, the current
    packet is enqueued into the offline buffer instead of being lost.
    """
    if buffer_count() > 0:
        flush_buffer()

    if _post_telemetry(payload, source=source):
        _set_network_online(True)
        return True
    else:
        enqueue_packet(payload)
        _set_network_online(False)
        return False


# Tracks whether the primary (Wi-Fi/cellular -> Supabase) path is currently
# reachable, so LoRa can act as a genuine FALLBACK transport per thesis
# objective 3, rather than an always-on parallel channel.
network_status_lock = threading.Lock()
network_status = {"online": True}


def _set_network_online(is_online):
    with network_status_lock:
        network_status["online"] = is_online


def is_network_online():
    with network_status_lock:
        return network_status["online"]


def build_lora_packet_from_payload(payload):
    """Builds the same compact CSV LoRa packet as build_lora_packet(), but
    reads its values from an already-built Supabase JSON payload — used
    when the LoRa fallback fires from inside the uploader thread, which
    only has the payload dict, not the raw sensor variables."""
    bio = payload["biometrics"]
    loc = payload["location"]
    fields = [
        time.strftime("%H:%M:%S"),
        f"{bio['ear']:.2f}" if bio["ear"] is not None else "",
        f"{bio['mar']:.2f}" if bio["mar"] is not None else "",
        f"{bio['perclos'] / 100:.2f}" if bio["perclos"] is not None else "",
        f"{bio['headPitch']:.1f}" if bio["headPitch"] is not None else "",
        payload["driverState"],
        f"{loc['lat']:.5f}" if loc["lat"] is not None else "",
        f"{loc['lng']:.5f}" if loc["lng"] is not None else "",
        "1" if loc["hasFix"] else "0",
        f"{1.0 - bio['alcoholRiskScore']:.2f}" if bio["alcoholRiskScore"] is not None else "",
        "",
    ]
    return ",".join(fields)


def classify_driver_state(avg_ear, eye_closed_frames, perclos, pitch, alcohol_ratio):
    """
    Maps your existing detection signals onto the four driverState values
    the ingest-telemetry contract expects: NORMAL, DROWSY, CRITICAL_FATIGUE,
    ALCOHOL_ALERT. Priority order: CRITICAL_FATIGUE > ALCOHOL_ALERT > DROWSY > NORMAL.
    """
    critical = (
        eye_closed_frames >= EAR_CONSEC_FRAMES
        or (perclos is not None and perclos >= PERCLOS_CRITICAL_THRESHOLD)
        or (pitch is not None and abs(pitch) >= HEAD_PITCH_CRITICAL_THRESHOLD)
    )
    if critical:
        return "CRITICAL_FATIGUE"

    if alcohol_ratio is not None and alcohol_ratio < MQ3_ALERT_RATIO_THRESHOLD:
        return "ALCOHOL_ALERT"

    drowsy = (
        (perclos is not None and perclos >= PERCLOS_THRESHOLD)
        or (pitch is not None and abs(pitch) >= HEAD_PITCH_THRESHOLD)
    )
    if drowsy:
        return "DROWSY"

    return "NORMAL"


def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


def get_cpu_load():
    if HAVE_PSUTIL:
        return round(psutil.cpu_percent(interval=None), 1)
    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        return round(min(100.0, (load1 / cores) * 100.0), 1)
    except Exception:
        return None


def get_wifi_info():
    """Returns (ssid, rssi_dbm) for wlan0, or (None, None) if unavailable.

    Uses nmcli for the SSID and /proc/net/wireless for the signal level,
    because iwgetid/iwconfig/iw are not installed on minimal Bookworm
    images — the previous implementation silently returned (None, None),
    blanking wifiSsid/wifiRssi in every telemetry packet."""
    ssid = get_current_ssid()

    rssi = None
    try:
        with open("/proc/net/wireless") as fh:
            for line in fh:
                if not line.lstrip().startswith(NM_WIFI_IFACE + ":"):
                    continue
                # e.g. " wlan0: 0000  47.  -63.  -256  0  0  ..."
                #           [status, link, level_dbm, noise, ...]
                fields = line.split(":", 1)[1].split()
                if len(fields) >= 3:
                    rssi = int(float(fields[2].rstrip(".")))
                break
    except Exception:
        rssi = None

    return ssid, rssi


# ---------------------------
# MANUAL WI-FI SWITCH (NetworkManager)
# ---------------------------
# Shared by the dashboard button. Matches profiles by *SSID* rather than by
# connection name, because the two often differ (the home profile here is
# named "preconfigured", not "Redmi 14C"). Uses nmcli instead of iwgetid/
# iwconfig, which are not present on minimal Bookworm images.
NM_WIFI_IFACE = "wlan0"


def _nmcli(args, timeout=20, use_sudo=True):
    """Run `nmcli`. Returns (returncode, stdout, stderr).

    Read-only queries work unprivileged (use_sudo=False) and avoid a sudo
    round-trip on the telemetry hot path; profile create/modify/activate
    needs root, which the service user has via passwordless sudo."""
    try:
        cmd = (["sudo", "-n"] if use_sudo else []) + ["nmcli"] + [str(a) for a in args]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:  # noqa: BLE001 — surface any failure to the caller
        return 1, "", str(exc)


def get_current_ssid():
    """SSID currently associated on wlan0, or None if not connected."""
    rc, name, _ = _nmcli(["-g", "GENERAL.CONNECTION", "device", "show", NM_WIFI_IFACE],
                         use_sudo=False)
    if rc != 0 or not name or name == "--":
        return None
    rc2, ssid, _ = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", name],
                          use_sudo=False)
    return ssid.strip() or None


def _find_connection_by_ssid(ssid):
    """Return the NetworkManager connection *name* whose wireless SSID is
    `ssid`, or None."""
    if not ssid:
        return None
    rc, out, _ = _nmcli(["-t", "-f", "NAME,TYPE", "connection", "show"], use_sudo=False)
    if rc != 0:
        return None
    for line in out.splitlines():
        name, _, ctype = line.rpartition(":")
        if ctype != "802-11-wireless" or not name:
            continue
        rc2, s, _ = _nmcli(["-g", "802-11-wireless.ssid", "connection", "show", name],
                           use_sudo=False)
        if rc2 == 0 and s.strip() == ssid:
            return name
    return None


def ensure_switch_profile():
    """Ensure a saved profile exists for the secondary SSID, with autoconnect
    disabled so it can never take over the boot network. Returns (ok, name_or_error)."""
    if not WIFI_SWITCH_TARGET_SSID:
        return False, "WIFI_SWITCH_TARGET_SSID is not configured"
    name = _find_connection_by_ssid(WIFI_SWITCH_TARGET_SSID)
    if name:
        # Belt-and-braces: make sure it can never hijack the boot network.
        _nmcli(["connection", "modify", name, "connection.autoconnect", "no"])
        return True, name
    rc, out, err = _nmcli([
        "connection", "add",
        "type", "wifi",
        "con-name", WIFI_SWITCH_TARGET_SSID,
        "ifname", NM_WIFI_IFACE,
        "wifi.ssid", WIFI_SWITCH_TARGET_SSID,
        "wifi-sec.key-mgmt", "wpa-psk",
        "wifi-sec.psk", WIFI_SWITCH_TARGET_PASSWORD,
        "connection.autoconnect", "no",
        "ipv4.method", "auto",
        "ipv6.method", "auto",
    ])
    if rc != 0:
        return False, (err or out or "nmcli connection add failed")
    return True, WIFI_SWITCH_TARGET_SSID


def schedule_wifi_switch(profile_name, delay=2.0):
    """Fire-and-forget: wait `delay` seconds, then activate the named profile.

    Runs detached via a transient systemd unit so the switch still completes
    after the dashboard HTTP request is cut off (the Pi leaves the current
    subnet the moment the new profile comes up)."""
    cmd = f"sleep {float(delay):.1f}; nmcli connection up {shlex.quote(profile_name)}"
    unit = f"tg-wifi-switch-{int(time.time())}"
    try:
        subprocess.Popen(
            ["sudo", "-n", "systemd-run", "--no-block", "--collect",
             "--unit", unit, "/bin/sh", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"Wi-Fi switch: failed to schedule -> {exc}")
        return False


def build_telemetry_payload(ear, mar, perclos, pitch, yaw, roll, location, alcohol,
                             driver_state, transport_mode="WIFI", test_state=None):
    """Builds the full canonical JSON payload matching Section 6.2 of the
    integration guide, field-for-field.

    `test_state` is only ever set by the gated scripted-state input; when it is
    not None the payload carries an explicit testInjection marker."""
    wifi_ssid, wifi_rssi = get_wifi_info() if transport_mode == "WIFI" else (None, None)

    # Approximate a 10-bit ADC-equivalent reading for schema compatibility,
    # since we're using an ADS1115 (16-bit) rather than the document's MCP3008.
    alcohol_voltage = alcohol.get("voltage")
    alcohol_raw_adc = None
    if alcohol_voltage is not None:
        alcohol_raw_adc = int(round((alcohol_voltage / MQ3_VCC) * 1023))
    alcohol_risk_score = None
    if alcohol.get("ratio") is not None:
        alcohol_risk_score = round(max(0.0, min(1.0, 1.0 - alcohol["ratio"])), 2)

    payload = {
        "deviceId": DEVICE_ID,
        "messageId": f"rpi-{uuid.uuid4()}",
        "timestamp": int(time.time() * 1000),
        "driverState": driver_state,
        "location": {
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
            "speed": _knots_to_kmh(location.get("speed_knots")),
            "heading": None,
            "satelliteCount": location.get("sats_used"),
            "satellitesInView": location.get("sats_in_view"),
            "bestSnr": location.get("best_snr"),
            "fixQuality": location.get("fix_quality"),
            "hasFix": bool(location.get("fix_valid")),
        },
        "biometrics": {
            "ear": round(ear, 3) if ear is not None else None,
            "mar": round(mar, 3) if mar is not None else None,
            "perclos": round(perclos * 100, 1) if perclos is not None else None,
            "headPitch": round(pitch, 1) if pitch is not None else None,
            "headYaw": round(yaw, 1) if yaw is not None else None,
            "headRoll": round(roll, 1) if roll is not None else None,
            "alcoholVoltage": round(alcohol_voltage, 2) if alcohol_voltage is not None else None,
            "alcoholRawADC": alcohol_raw_adc,
            "alcoholRiskScore": alcohol_risk_score,
            "driverState": driver_state,
        },
        "hardware": {
            "cpuLoad": get_cpu_load(),
            "cpuTemp": get_cpu_temp(),
            "powerWatts": None,      # requires INA219 or similar, not yet installed
            "batteryVoltage": None,  # requires a voltage divider/ADC channel, not yet installed
            "buzzerActive": get_current_actuator()["buzzer_active"],
            "lcdMessage": get_current_actuator()["lcd_message"],
        },
        "transport": {
            "mode": transport_mode,
            "wifiRssi": wifi_rssi,
            "wifiSsid": wifi_ssid,
            "loraHopCount": 0,
            "loraRssi": None,
            "loraSnr": None,
            "relayedVia": None,
            "bufferedQueueCount": buffer_count(),
        },
    }
    if test_state is not None:
        # Provenance marker for scripted evaluation input. Presence of this
        # object means driverState was overridden, not measured.
        payload["testInjection"] = {
            "enabled": True,
            "source": "test-state-input",
            "state": test_state,
            "note": "Scripted evaluation input; not a measured detection.",
        }
    return payload


def _knots_to_kmh(knots):
    if knots is None:
        return None
    return round(knots * 1.852, 1)


# ---------------------------
# SHARED GPS STATE (updated by background thread)
# ---------------------------
gps_lock = threading.Lock()


def _format_age(seconds):
    """Compact human-readable age for a stale position: '45s', '2m 14s',
    '1h 3m', '3d 4h'. Mirrors fmtAge() in the dashboard JS, so the video
    overlay and the web page never describe the same number differently."""
    if seconds is None:
        return "unknown"
    seconds = int(round(max(0.0, float(seconds))))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {mins}m"
    days, hrs = divmod(hours, 24)
    return f"{days}d {hrs}h"


def _meters_between(lat1, lon1, lat2, lon2):
    """Rough equirectangular distance in metres — accurate enough for a
    'has the vehicle moved?' threshold, and needs no extra dependency."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1) * math.cos((phi1 + phi2) / 2.0)
    return 6371000.0 * math.sqrt(d_phi * d_phi + d_lambda * d_lambda)


# Mirrors what the file on disk currently holds, so a repeat fix for the same
# spot does not trigger a write. Seeded from the file at startup (below).
_gps_last_known_on_disk = {"latitude": None, "longitude": None, "timestamp": None}


def _load_gps_last_known():
    """Load the last known GPS fix from disk into latest_gps at startup.
    This eliminates the 'no fix yet' period on reboot — the module can
    immediately report the last seen position (flagged as stale/using_last_known)
    until a new live fix arrives.  Fails silently if the file is absent or corrupt."""
    try:
        with open(GPS_LAST_KNOWN_PATH) as f:
            data = json.load(f)
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        ts  = float(data["timestamp"])
        # Sanity bounds — reject obviously corrupt values
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return lat, lon, ts
    except Exception:
        pass
    return None, None, None


def _save_gps_last_known(lat, lon, timestamp):
    """Persist the most recent valid GPS fix to disk so the next boot
    can pre-populate last_known without waiting for a fresh satellite lock.

    Throttled: the GPS streams RMC sentences continuously, but the file only
    has to be fresh enough for the next boot, so it is rewritten at most once
    per GPS_LAST_KNOWN_MIN_INTERVAL_SEC — or sooner once the vehicle has
    actually moved GPS_LAST_KNOWN_MIN_MOVE_M.

    The write goes to a sibling .tmp file that is then renamed into place with
    os.replace(), which is atomic: a crash, a full disk or a kill mid-write
    can never leave a half-written file for the next boot to parse — a
    reader sees either the previous file or the new one.
    """
    on_disk = _gps_last_known_on_disk
    prev_ts, prev_lat, prev_lon = (on_disk["timestamp"], on_disk["latitude"],
                                   on_disk["longitude"])
    if prev_ts is not None and prev_lat is not None:
        if timestamp - prev_ts < GPS_LAST_KNOWN_MIN_INTERVAL_SEC:
            if _meters_between(prev_lat, prev_lon, lat, lon) < GPS_LAST_KNOWN_MIN_MOVE_M:
                return  # written recently enough and not going anywhere
    tmp_path = GPS_LAST_KNOWN_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump({"latitude": lat, "longitude": lon, "timestamp": timestamp}, f)
            f.flush()
            os.fsync(f.fileno())   # do not let a power cut win the race
        os.replace(tmp_path, GPS_LAST_KNOWN_PATH)
        on_disk.update(timestamp=timestamp, latitude=lat, longitude=lon)
    except Exception:
        # non-critical — the GPS_LAST_KNOWN_PATH file is still intact, because
        # nothing was ever written to that path directly
        try:
            os.remove(tmp_path)
        except OSError:
            pass


_boot_lat, _boot_lon, _boot_ts = _load_gps_last_known()
if _boot_lat is not None:
    # There is already a position on disk: keep it until the vehicle moves or
    # the refresh interval expires, instead of rewriting it on the first fix.
    _gps_last_known_on_disk.update(latitude=_boot_lat, longitude=_boot_lon,
                                   timestamp=_boot_ts)

latest_gps = {
    "latitude": None,
    "longitude": None,
    "speed_knots": None,
    "fix_valid": False,
    "last_update": None,
    # Separately tracked so a lost fix never loses the vehicle's last
    # confirmed position — only ever updated on an actual valid fix.
    # Pre-populated from disk so the first boot is not a blank slate.
    "last_known_latitude": _boot_lat,
    "last_known_longitude": _boot_lon,
    "last_known_time": _boot_ts,
    # NMEA diagnostics. Populated only when the module emits GGA/GSV (see
    # enable_required_nmea()); they stay None on RMC-only modules.
    "sats_used": None,       # GGA: satellites used in the fix
    "sats_in_view": None,    # GSV: satellites currently in view
    "best_snr": None,        # GSV: best C/N0 seen in the current sweep
    "fix_quality": None,     # GGA: 0 = none, 1 = GPS, 2 = DGPS
    "hdop": None,
    "altitude_m": None,
    "gsv_at": None,          # wall time of the last GSV sentence
}
del _boot_lat, _boot_lon, _boot_ts  # clean up boot-time temporaries


def _safe_float(value):
    """float() that returns None instead of raising on blank NMEA fields."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ubx_checksum(data):
    """u-blox 8-bit Fletcher checksum (CK_A, CK_B)."""
    ck_a = ck_b = 0
    for byte in data:
        ck_a = (ck_a + byte) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return ck_a, ck_b


def _ubx_cfg_msg(ser, msg_class, msg_id, rate):
    """Send UBX-CFG-MSG to set a message's output rate on the current port."""
    payload = bytes([msg_class, msg_id, rate])
    frame = bytes([0xB5, 0x62, 0x06, 0x01, len(payload), 0x00]) + payload
    ck_a, ck_b = _ubx_checksum(frame[2:])   # checksum covers class..payload
    ser.write(frame + bytes([ck_a, ck_b]))
    ser.flush()


def enable_required_nmea(ser):
    """Ask the module for the sentences this app reports on: GGA (satellites
    used + fix quality), GSV (satellites in view + SNR), GSA, VTG and RMC.
    Silently does nothing on modules that don't speak UBX."""
    for name, (cls, mid) in (("GGA", (0xF0, 0x00)), ("GSA", (0xF0, 0x02)),
                             ("GSV", (0xF0, 0x03)), ("RMC", (0xF0, 0x04)),
                             ("VTG", (0xF0, 0x05))):
        try:
            _ubx_cfg_msg(ser, cls, mid, 1)
        except Exception as e:
            print(f"GPS: could not request {name} sentences: {e}")
            return
    print("GPS: requested GGA/GSA/GSV/RMC/VTG NMEA output")


def gps_reader_thread():
    ser = None
    # A transient open failure must not kill GPS for the whole run.
    while ser is None:
        try:
            ser = serial.Serial(GPS_UART_PORT, GPS_BAUD_RATE, timeout=1)
        except Exception as e:
            print(f"GPS serial port could not be opened: {e} — retrying in 5s")
            time.sleep(5)

    if GPS_ENABLE_SENTENCES:
        enable_required_nmea(ser)

    while True:
        try:
            raw_line = ser.readline().decode("ascii", errors="ignore").strip()
        except serial.SerialException:
            continue

        if not raw_line.startswith("$"):
            continue

        try:
            msg = pynmea2.parse(raw_line)
        except pynmea2.ParseError:
            continue

        if isinstance(msg, pynmea2.types.talker.RMC):
            with gps_lock:
                if msg.status == "A":
                    latest_gps["latitude"] = msg.latitude
                    latest_gps["longitude"] = msg.longitude
                    latest_gps["speed_knots"] = msg.spd_over_grnd
                    latest_gps["fix_valid"] = True
                    # Record this as the new last-known-good position
                    latest_gps["last_known_latitude"] = msg.latitude
                    latest_gps["last_known_longitude"] = msg.longitude
                    latest_gps["last_known_time"] = time.time()
                    # Persist to disk — next boot will pre-load this immediately
                    _save_gps_last_known(msg.latitude, msg.longitude, latest_gps["last_known_time"])
                else:
                    latest_gps["fix_valid"] = False
                latest_gps["last_update"] = time.time()

        elif isinstance(msg, pynmea2.types.talker.GGA):
            with gps_lock:
                sats = None
                try:
                    sats = int(msg.num_sats) if msg.num_sats not in (None, "") else 0
                except (TypeError, ValueError):
                    sats = None
                if sats is not None:
                    latest_gps["sats_used"] = sats
                try:
                    quality = int(msg.gps_qual) if msg.gps_qual not in (None, "") else 0
                except (TypeError, ValueError):
                    quality = 0
                latest_gps["fix_quality"] = quality
                latest_gps["hdop"] = _safe_float(msg.horizontal_dil)
                latest_gps["altitude_m"] = _safe_float(msg.altitude)
                # A GGA with quality > 0 is a live fix even if the RMC status
                # byte has not caught up yet this second.
                if quality > 0 and msg.latitude and msg.longitude:
                    latest_gps["latitude"] = msg.latitude
                    latest_gps["longitude"] = msg.longitude
                    latest_gps["fix_valid"] = True

        elif isinstance(msg, pynmea2.types.talker.GSV):
            with gps_lock:
                try:
                    latest_gps["sats_in_view"] = (
                        int(msg.num_sv_in_view) if msg.num_sv_in_view not in (None, "") else 0
                    )
                except (TypeError, ValueError):
                    pass
                now = time.time()
                # GSV spans several sentences per sweep: start a fresh best-SNR
                # window when this sentence opens a new sweep.
                if latest_gps["gsv_at"] is None or now - latest_gps["gsv_at"] > 2.0:
                    latest_gps["best_snr"] = None
                latest_gps["gsv_at"] = now
                snrs = [_safe_float(getattr(msg, f"snr_{i}", None)) for i in range(1, 5)]
                snrs = [s for s in snrs if s is not None]
                if snrs:
                    best = max(snrs)
                    prev = latest_gps["best_snr"]
                    latest_gps["best_snr"] = best if prev is None else max(prev, best)


def get_current_location():
    """
    Returns the location to use right now. If a live fix is currently
    available, returns it as-is. If not, falls back to the last known
    good fix (if the GPS has ever locked at all), and flags the result
    with 'using_last_known' and 'location_age_sec' so callers (overlay,
    telemetry, LoRa, logging) know the position may be stale — 'fix_valid'
    stays False in that case, since it genuinely isn't a live fix.
    """
    with gps_lock:
        snapshot = dict(latest_gps)

    if snapshot["fix_valid"]:
        snapshot["using_last_known"] = False
        snapshot["location_age_sec"] = 0.0
        return snapshot

    if snapshot["last_known_latitude"] is not None:
        snapshot["latitude"] = snapshot["last_known_latitude"]
        snapshot["longitude"] = snapshot["last_known_longitude"]
        snapshot["using_last_known"] = True
        snapshot["location_age_sec"] = (
            time.time() - snapshot["last_known_time"]
            if snapshot["last_known_time"] is not None else None
        )
    else:
        # GPS has never locked at all yet — genuinely nothing to fall back to
        snapshot["using_last_known"] = False
        snapshot["location_age_sec"] = None

    return snapshot


# ---------------------------
# SHARED ALCOHOL STATE (updated by background thread)
# ---------------------------
alcohol_lock = threading.Lock()
latest_alcohol = {
    "voltage": None,
    "rs": None,
    "ratio": None,
    "ppm": None,
    "alert": False,
    "last_update": None,
}


def _voltage_to_rs(voltage):
    if voltage <= 0:
        return float("inf")
    return (MQ3_VCC - voltage) * MQ3_RL_VALUE / voltage


def _rs_to_ppm(rs, r0):
    ratio = rs / r0
    if ratio <= 0:
        return 0.0, ratio
    ppm = MQ3_CURVE_A * (ratio ** MQ3_CURVE_B)
    return ppm, ratio


def alcohol_reader_thread():
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        ads = ADS.ADS1115(i2c)
        channel = AnalogIn(ads, ads1x15.Pin.A0)
    except Exception as e:
        print(f"ADS1115/MQ-3 could not be initialized: {e}")
        return

    while True:
        voltage = channel.voltage
        rs = _voltage_to_rs(voltage)
        ppm, ratio = _rs_to_ppm(rs, MQ3_R0)
        alert = ratio < MQ3_ALERT_RATIO_THRESHOLD

        with alcohol_lock:
            latest_alcohol["voltage"] = voltage
            latest_alcohol["rs"] = rs
            latest_alcohol["ratio"] = ratio
            latest_alcohol["ppm"] = ppm
            latest_alcohol["alert"] = alert
            latest_alcohol["last_update"] = time.time()

        time.sleep(MQ3_POLL_INTERVAL_SEC)


def get_current_alcohol():
    with alcohol_lock:
        return dict(latest_alcohol)


# ---------------------------
# LORA TRANSMISSION (ATK-LORA-01) — kept as a separate low-rate transport
# ---------------------------
def setup_lora():
    try:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(LORA_M0_PIN, GPIO.OUT)
        GPIO.setup(LORA_M1_PIN, GPIO.OUT)
        GPIO.output(LORA_M0_PIN, GPIO.LOW)
        GPIO.output(LORA_M1_PIN, GPIO.LOW)
        time.sleep(0.1)

        lora_ser = serial.Serial(LORA_UART_PORT, LORA_BAUD_RATE, timeout=1)
        print("LoRa module ready on", LORA_UART_PORT)
        return lora_ser
    except Exception as e:
        print(f"LoRa module could not be initialized, continuing without it: {e}")
        return None


def build_lora_packet(ear, mar, perclos, pitch, driver_state, location, alcohol):
    fields = [
        time.strftime("%H:%M:%S"),
        f"{ear:.2f}" if ear is not None else "",
        f"{mar:.2f}" if mar is not None else "",
        f"{perclos:.2f}" if perclos is not None else "",
        f"{pitch:.1f}" if pitch is not None else "",
        driver_state,
        f"{location['latitude']:.5f}" if location["latitude"] is not None else "",
        f"{location['longitude']:.5f}" if location["longitude"] is not None else "",
        "1" if location["fix_valid"] else "0",
        f"{alcohol['ratio']:.2f}" if alcohol["ratio"] is not None else "",
        f"{alcohol['ppm']:.1f}" if alcohol["ppm"] is not None else "",
    ]
    return ",".join(fields)


def lora_send(lora_ser, packet):
    if lora_ser is None:
        return
    try:
        lora_ser.write((packet + "\n").encode())
    except serial.SerialException as e:
        print(f"LoRa send failed: {e}")


# ---------------------------
# PERFORMANCE: background workers so the camera loop never blocks
# ---------------------------
class ThreadedCamera:
    """
    Continuously grabs frames on its own thread and always keeps only the
    MOST RECENT frame available. The main loop never waits on cap.read()
    — it just grabs whatever's latest, which removes camera I/O stalls
    from the inference loop entirely.
    """
    def __init__(self, src=0, width=None, height=None):
        self.src = src
        self.width = width
        self.height = height
        self.cap = self._open(src, width, height)
        self.lock = threading.Lock()
        self.frame = None
        self.ok = self.cap.isOpened()
        self.stopped = False
        self.consecutive_failures = 0
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    @staticmethod
    def _open(src, width=None, height=None):
        cap = cv2.VideoCapture(src)
        if width:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        return cap

    def _reopen(self):
        """A UVC camera that failed mid-stream often recovers on reopen."""
        try:
            self.cap.release()
        except Exception:
            pass
        time.sleep(CAMERA_REOPEN_DELAY_SEC)
        try:
            self.cap = self._open(self.src, self.width, self.height)
            if self.cap.isOpened():
                print("[CAMERA] reopened the capture device")
                self.consecutive_failures = 0
            else:
                print("[CAMERA] reopen failed; will keep retrying")
        except Exception as e:
            print(f"[CAMERA] reopen error: {e}")

    def _update(self):
        while not self.stopped:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.frame = frame
                    self.ok = True
                self.consecutive_failures = 0
                continue

            # Read failed: NEVER spin here. A tight retry loop used to peg a
            # core at 100% CPU and silently stall the whole app (no telemetry,
            # no GPS). Back off, then periodically try to reopen the device.
            self.consecutive_failures += 1
            with self.lock:
                self.ok = False
            if self.consecutive_failures == 1:
                print("[CAMERA] frame read failed; backing off and retrying")
            if self.consecutive_failures % CAMERA_REOPEN_AFTER_FAILURES == 0:
                print(f"[CAMERA] {self.consecutive_failures} consecutive read failures "
                      f"— attempting to reopen the device")
                self._reopen()
            time.sleep(min(CAMERA_RETRY_BACKOFF_SEC * self.consecutive_failures,
                           CAMERA_RETRY_BACKOFF_MAX_SEC))

    def read(self):
        with self.lock:
            if self.frame is None:
                return False, None
            return True, self.frame.copy()

    def release(self):
        self.stopped = True
        self.thread.join(timeout=1)
        self.cap.release()


# Queues decouple slow network/radio I/O from the camera/inference loop.
# Bounded with drop-oldest behaviour: if the network is slow we discard the
# OLDEST telemetry rather than backing up an ever-growing queue in RAM (the
# offline SQLite buffer already handles real persistence/retry). The cap is a
# safety margin only — the alert path is rate-limited at the producer so a
# sustained alarm should not reach it.
telemetry_queue = queue.Queue(maxsize=TELEMETRY_QUEUE_MAXSIZE)
lora_queue = queue.Queue(maxsize=5)


def telemetry_uploader_thread():
    while True:
        payload = telemetry_queue.get()
        sent = send_telemetry(payload)
        if sent:
            print(f"[SENT via WiFi/Cellular] {payload['driverState']} | "
                  f"msgId: {payload['messageId'][:16]}...")
        else:
            print(f"[OFFLINE] {payload['driverState']} | primary path down, "
                  f"buffered locally ({buffer_count()} pending) — falling back to LoRa")
            # Objective 3: LoRa forwarding when cellular/WiFi is unavailable
            queue_put_latest(lora_queue, build_lora_packet_from_payload(payload))
        telemetry_queue.task_done()


def lora_sender_thread(lora_ser):
    while True:
        packet = lora_queue.get()
        lora_send(lora_ser, packet)
        lora_queue.task_done()


def queue_put_latest(q, item, trace_label=None):
    """Puts an item on a queue, dropping the oldest pending item first if
    the queue is full, so we always send the freshest data, not the
    stalest, when the network/radio has fallen behind."""
    try:
        q.put_nowait(item)
    except queue.Full:
        dropped = None
        try:
            dropped = q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass
        if trace_label:
            trace_event(
                ev="dropped",
                label=trace_label,
                messageId=(dropped or {}).get("messageId") if isinstance(dropped, dict) else None,
            )


# ---------------------------
# LCD + BUZZER (driver-facing local alerts, thesis objective 5)
# ---------------------------
DRIVER_STATE_MESSAGES = {
    "NORMAL": ("Status: Normal", "Drive safe"),
    "DROWSY": ("!! DROWSY !!", "Take a break"),
    "CRITICAL_FATIGUE": ("!! WAKE UP !!", "Pull over now"),
    "ALCOHOL_ALERT": ("ALCOHOL DETECT", "Do not drive"),
}

actuator_lock = threading.Lock()
latest_actuator = {"buzzer_active": False, "lcd_message": None}
actuator_queue = queue.Queue(maxsize=5)


def get_ip_address():
    """
    Returns the Pi's LAN IP address, using the standard 'connect to a
    public IP over UDP without actually sending data' trick to find
    which local interface/address would be used for outbound traffic.
    Falls back to a hostname-based lookup, then '127.0.0.1' if both fail
    (e.g. no network connection yet).
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def setup_lcd():
    if not HAVE_RPLCD:
        print("RPLCD not installed (pip install RPLCD) — running without the LCD.")
        return None
    try:
        lcd = CharLCD(
            numbering_mode=GPIO.BCM,
            cols=LCD_COLS, rows=LCD_ROWS,
            pin_rs=LCD_RS_PIN, pin_e=LCD_E_PIN,
            pins_data=[LCD_D4_PIN, LCD_D5_PIN, LCD_D6_PIN, LCD_D7_PIN],
        )
        ip = get_ip_address()
        lcd.clear()
        lcd.write_string("TransitGuard")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(f"IP:{ip}"[:LCD_COLS])
        print(f"LCD boot screen showing IP address: {ip}")
        return lcd
    except Exception as e:
        print(f"LCD could not be initialized, continuing without it: {e}")
        return None


def setup_buzzer():
    try:
        GPIO.setup(BUZZER_PIN, GPIO.OUT)
        GPIO.output(BUZZER_PIN, GPIO.LOW if BUZZER_ACTIVE_HIGH else GPIO.HIGH)
        return True
    except Exception as e:
        print(f"Buzzer could not be initialized, continuing without it: {e}")
        return False


def set_buzzer(is_on):
    try:
        if BUZZER_ACTIVE_HIGH:
            GPIO.output(BUZZER_PIN, GPIO.HIGH if is_on else GPIO.LOW)
        else:
            GPIO.output(BUZZER_PIN, GPIO.LOW if is_on else GPIO.HIGH)
    except Exception:
        pass  # buzzer not initialized, ignore silently


def display_on_lcd(lcd, line1, line2):
    if lcd is None:
        return
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(line1[:LCD_COLS])
        lcd.cursor_pos = (1, 0)
        lcd.write_string(line2[:LCD_COLS])
    except Exception as e:
        print(f"LCD write failed: {e}")


def get_current_actuator():
    with actuator_lock:
        return dict(latest_actuator)


def actuator_thread(lcd, buzzer_ready):
    """
    Runs LCD writes and buzzer switching off the main camera loop, so a
    slow LCD update (a few tens of ms over bit-banged GPIO) never causes
    a dropped or delayed video frame.
    """
    while True:
        driver_state = actuator_queue.get()
        line1, line2 = DRIVER_STATE_MESSAGES.get(driver_state, ("Status: Unknown", ""))
        display_on_lcd(lcd, line1, line2)

        buzzer_should_be_on = driver_state != "NORMAL"
        if buzzer_ready:
            set_buzzer(buzzer_should_be_on)

        with actuator_lock:
            latest_actuator["buzzer_active"] = buzzer_should_be_on and buzzer_ready
            latest_actuator["lcd_message"] = f"{line1} | {line2}"

        actuator_queue.task_done()


# ---------------------------
# LIVE BROWSER DASHBOARD (video feed + live metrics over the network)
# ---------------------------
dashboard_lock = threading.Lock()
latest_frame_jpeg = None
latest_metrics = {
    "driver_state": "NORMAL",
    "ear": None, "mar": None, "perclos": None, "pitch": None,
    "latitude": None, "longitude": None, "fix_valid": False,
    "using_last_known": False, "location_age_sec": None,
    "sats_used": None, "sats_in_view": None, "best_snr": None, "fix_quality": None,
    "alcohol_ratio": None, "alcohol_ppm": None,
    "network_online": True, "buffer_count": 0,
    "alert_coalesced": 0,
    "buzzer_active": False, "lcd_message": None,
    "frame_width": None, "frame_height": None,
    "pi_temperature": None, "pi_throttled": None, "pi_thermal_note": "",
    "updated_at": None,
}

dashboard_app = Flask(__name__)


# ── Pi SoC temperature / throttling ─────────────────────────────────────────
# thermal_zone0 reports millidegree Celsius and needs no helper binary, so the
# reading works even where vcgencmd is unavailable. vcgencmd is only used for
# the "sticky" throttling flags.
_THERMAL_ZONE_PATH = "/sys/class/thermal/thermal_zone0/temp"
_VCGENCMD = "/usr/bin/vcgencmd"

_pi_thermal_cache = {"at": 0.0, "temp": None, "throttled": None, "note": ""}
_PI_THERMAL_TTL = 1.0        # seconds; the dashboard polls about once a second

# Colour levels in °C. The Pi's own soft temperature limit is 85 °C.
PI_TEMP_COOL = 70.0
PI_TEMP_WARM = 80.0
PI_TEMP_HOT = 85.0

# vcgencmd get_throttled bit meanings (BCM2835/2836 sticky flags).
_THROTTLE_BITS = (
    (0x1,     "under-voltage detected"),
    (0x2,     "arm frequency capped"),
    (0x4,     "currently throttled"),
    (0x8,     "soft temperature limit active"),
    (0x10000, "under-voltage has occurred"),
    (0x20000, "arm frequency capping has occurred"),
    (0x40000, "throttling has occurred"),
    (0x80000, "soft temperature limit has occurred"),
)


def _read_thermal_zone():
    """SoC temperature in °C from sysfs, or None if unavailable."""
    try:
        with open(_THERMAL_ZONE_PATH, "r", encoding="ascii") as fh:
            return round(int(fh.read().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def _read_throttled_flags():
    """Decode `vcgencmd get_throttled` into (raw, [active], [occurred])."""
    try:
        out = subprocess.run([_VCGENCMD, "get_throttled"], capture_output=True,
                             text=True, timeout=3)
        m = re.search(r"throttled=(0x[0-9a-fA-F]+)", out.stdout)
        if not m:
            return None, [], []
        raw = int(m.group(1), 16)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, [], []
    active = [desc for bit, desc in _THROTTLE_BITS if raw & bit and bit < 0x10000]
    occurred = [desc for bit, desc in _THROTTLE_BITS if raw & bit and bit >= 0x10000]
    return raw, active, occurred


def read_pi_thermal():
    """Cached (temp °C, throttled hex string, human note). Cheap to poll."""
    now = time.time()
    if now - _pi_thermal_cache["at"] < _PI_THERMAL_TTL:
        return (_pi_thermal_cache["temp"], _pi_thermal_cache["throttled"],
                _pi_thermal_cache["note"])
    temp = _read_thermal_zone()
    raw, active, occurred = _read_throttled_flags()
    if temp is None:
        note = "thermal zone unavailable"
    elif active:
        note = "; ".join(active)
    elif occurred:
        note = "previously: " + "; ".join(occurred)
    else:
        note = ""
    throttled = f"0x{raw:x}" if raw is not None else None
    _pi_thermal_cache.update(at=now, temp=temp, throttled=throttled, note=note)
    return temp, throttled, note


def pi_temp_level(temp):
    """'cool' | 'warm' | 'hot' | 'critical' | None — drives the dashboard colour."""
    if temp is None:
        return None
    if temp >= PI_TEMP_HOT:
        return "critical"
    if temp >= PI_TEMP_WARM:
        return "hot"
    if temp >= PI_TEMP_COOL:
        return "warm"
    return "cool"


def _put_hud_text(frame, text, org, scale, color, thickness):
    """Draw HUD text over a dark outline so it stays readable on any frame."""
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale,
                (0, 0, 0), thickness + 2, cv2.LINE_AA)
    cv2.putText(frame, text, org, cv2.FONT_HERSHEY_SIMPLEX, scale,
                color, thickness, cv2.LINE_AA)


def update_dashboard_frame(frame):
    """Encodes the current annotated frame to JPEG and stores it for
    streaming. Called once per main-loop iteration."""
    global latest_frame_jpeg
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if ok:
        with dashboard_lock:
            latest_frame_jpeg = buf.tobytes()
            # Publish the true frame size so the UI label can never claim a
            # resolution the camera is not actually delivering.
            latest_metrics["frame_width"] = int(frame.shape[1])
            latest_metrics["frame_height"] = int(frame.shape[0])


def update_dashboard_metrics(**kwargs):
    with dashboard_lock:
        latest_metrics.update(kwargs)
        latest_metrics["updated_at"] = time.strftime("%H:%M:%S")


def _mjpeg_generator():
    while True:
        with dashboard_lock:
            frame_bytes = latest_frame_jpeg
        if frame_bytes is not None:
            yield (b"--frame\r\n"
                   b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n")
        time.sleep(0.05)  # ~20 FPS cap on the stream, independent of inference rate


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <title>TransitGuard Live Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        /* ── CSS custom properties for theme switching ── */
        :root[data-theme="dark"] {
            --bg:          #111214;
            --bg-panel:    #1a1c1f;
            --bg-card:     #212428;
            --border:      #2e3035;
            --text-primary:#e8eaed;
            --text-muted:  #7a7f8a;
            --accent:      #4fc3f7;
            --accent-dim:  rgba(79,195,247,0.12);
            --cam-border:  #2e3035;
            --toggle-bg:   #2e3035;
        }
        :root[data-theme="light"] {
            --bg:          #e8eaed;
            --bg-panel:    #f5f6f8;
            --bg-card:     #ffffff;
            --border:      #d1d4da;
            --text-primary:#1a1c20;
            --text-muted:  #6b7280;
            --accent:      #0284c7;
            --accent-dim:  rgba(2,132,199,0.10);
            --cam-border:  #d1d4da;
            --toggle-bg:   #d1d4da;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, 'Segoe UI', sans-serif;
            background: var(--bg);
            color: var(--text-primary);
            min-height: 100vh;
            padding: 0;
        }

        /* ── Top header bar ── */
        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 10px 20px;
            background: var(--bg-panel);
            border-bottom: 1px solid var(--border);
        }
        .topbar-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }
        .logo {
            font-size: 13px;
            font-weight: 700;
            letter-spacing: 2px;
            text-transform: uppercase;
            color: var(--accent);
        }
        .live-badge {
            display: flex;
            align-items: center;
            gap: 5px;
            font-size: 11px;
            font-weight: 600;
            color: var(--text-muted);
            background: var(--accent-dim);
            padding: 3px 9px;
            border-radius: 4px;
            border: 1px solid var(--border);
        }
        .live-dot {
            width: 7px; height: 7px;
            border-radius: 50%;
            background: #ef4444;
            animation: blink 1.2s infinite;
        }
        @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }

        .topbar-right { display: flex; align-items: center; gap: 12px; }

        .device-label {
            font-size: 12px;
            color: var(--text-muted);
            font-weight: 600;
            letter-spacing: 0.5px;
        }

        /* ── Theme toggle button ── */
        .theme-toggle {
            background: var(--toggle-bg);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 5px 14px;
            cursor: pointer;
            font-size: 13px;
            color: var(--text-primary);
            transition: background 0.2s;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .theme-toggle:hover { background: var(--accent-dim); border-color: var(--accent); }
        .toggle-icon { font-size: 15px; }

        /* ── Pi temperature nav button ── */
        .nav-temp {
            background: var(--toggle-bg);
            border: 1px solid var(--border);
            border-radius: 20px;
            padding: 5px 14px;
            cursor: pointer;
            font-size: 13px;
            font-weight: 600;
            color: var(--text-primary);
            text-decoration: none;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: background 0.2s, border-color 0.2s;
        }
        .nav-temp:hover { background: var(--accent-dim); border-color: var(--accent); }
        .nav-temp .tval { font-variant-numeric: tabular-nums; }
        .nav-temp.cool     { color: #4ade80; }
        .nav-temp.warm     { color: #fbbf24; }
        .nav-temp.hot      { color: #fb923c; }
        .nav-temp.critical { color: #f87171; }

        /* ── Wi-Fi switch card ── */
        .wifi-current {
            font-size: 13px;
            font-weight: 700;
            color: var(--text-primary);
            margin-bottom: 10px;
            word-break: break-word;
        }
        .wifi-btn {
            width: 100%;
            background: var(--accent-dim);
            border: 1px solid var(--accent);
            border-radius: 8px;
            padding: 8px 12px;
            cursor: pointer;
            font-size: 12px;
            font-weight: 700;
            color: var(--accent);
            transition: background 0.2s, color 0.2s;
        }
        .wifi-btn:hover:not(:disabled) { background: var(--accent); color: #fff; }
        .wifi-btn:disabled { opacity: 0.55; cursor: not-allowed; }
        .wifi-note {
            font-size: 10px;
            color: var(--text-muted);
            margin-top: 8px;
            line-height: 1.4;
        }

        /* ── Main layout ── */
        .main {
            display: grid;
            grid-template-columns: 1fr 300px;
            grid-template-rows: auto auto;
            gap: 12px;
            padding: 14px 20px 20px;
            height: calc(100vh - 53px);
        }

        /* ── Camera panel ── */
        .cam-panel {
            grid-row: 1 / 3;
            background: #000;
            border-radius: 10px;
            border: 1px solid var(--cam-border);
            overflow: hidden;
            position: relative;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .cam-panel img {
            width: 100%;
            height: 100%;
            /* contain, not cover: the JPEG carries a burned-in HUD, so the frame
               must never be cropped. The panel background is black, so any
               letterbox bars are invisible. */
            object-fit: contain;
            display: block;
        }
        /* HUD overlays on camera */
        .cam-overlay {
            position: absolute;
            top: 0; left: 0; right: 0; bottom: 0;
            pointer-events: none;
        }
        .cam-label {
            position: absolute;
            top: 12px; left: 14px;
            background: rgba(0,0,0,0.55);
            backdrop-filter: blur(4px);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 5px;
            padding: 4px 10px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            color: #e8eaed;
        }
        .cam-state-badge {
            position: absolute;
            top: 12px; right: 14px;
        }

        /* ── Right column panels ── */
        .right-col {
            display: flex;
            flex-direction: column;
            gap: 12px;
        }

        /* ── State card (top-right) ── */
        .state-card {
            background: var(--bg-panel);
            border-radius: 10px;
            border: 1px solid var(--border);
            padding: 14px 16px;
        }
        .state-title {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: var(--text-muted);
            margin-bottom: 10px;
        }
        .state-badge {
            font-size: 18px;
            font-weight: 800;
            letter-spacing: 1px;
            padding: 10px 14px;
            border-radius: 8px;
            text-align: center;
            width: 100%;
        }
        .NORMAL         { background: rgba(34,197,94,0.15);  color: #4ade80; border: 1px solid rgba(34,197,94,0.3); }
        .DROWSY         { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.3); }
        .CRITICAL_FATIGUE { background: rgba(239,68,68,0.15); color: #f87171; border: 1px solid rgba(239,68,68,0.3); }
        .ALCOHOL_ALERT  { background: rgba(168,85,247,0.15); color: #c084fc; border: 1px solid rgba(168,85,247,0.3); }

        /* ── Metrics card ── */
        .metrics-card {
            background: var(--bg-panel);
            border-radius: 10px;
            border: 1px solid var(--border);
            padding: 14px 16px;
            flex: 1;
            overflow-y: auto;
        }
        .metrics-title {
            font-size: 10px;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: var(--text-muted);
            margin-bottom: 12px;
        }
        .metric-row {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 7px 0;
            border-bottom: 1px solid var(--border);
            font-size: 13px;
        }
        .metric-row:last-child { border-bottom: none; }
        .metric-label {
            color: var(--text-muted);
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.3px;
            text-transform: uppercase;
        }
        .metric-value {
            color: var(--text-primary);
            font-weight: 700;
            font-size: 13px;
            font-variant-numeric: tabular-nums;
        }
        .metric-value.good   { color: #4ade80; }
        .metric-value.warn   { color: #fbbf24; }
        .metric-value.alert  { color: #f87171; }
        .metric-value.accent { color: var(--accent); }
        .metric-value.muted  { color: var(--text-muted); }
        /* Pi SoC temperature levels — shared with /temperature */
        .metric-value.cool     { color: #4ade80; }
        .metric-value.warm     { color: #fbbf24; }
        .metric-value.hot      { color: #fb923c; }
        .metric-value.critical { color: #f87171; }

        /* ── Bottom strip: system status ── */
        .status-strip {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 10px;
            grid-column: 1 / 3;
        }
        .status-chip {
            background: var(--bg-panel);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 10px 14px;
            display: flex;
            flex-direction: column;
            gap: 3px;
        }
        .chip-label {
            font-size: 9px;
            font-weight: 700;
            letter-spacing: 1.5px;
            text-transform: uppercase;
            color: var(--text-muted);
        }
        .chip-value {
            font-size: 14px;
            font-weight: 700;
            color: var(--text-primary);
            font-variant-numeric: tabular-nums;
        }
        .chip-value.on  { color: #f87171; }
        .chip-value.off { color: var(--text-muted); }
        .chip-value.online { color: #4ade80; }
        .chip-value.offline { color: #fbbf24; }
        .chip-value.cool     { color: #4ade80; }
        .chip-value.warm     { color: #fbbf24; }
        .chip-value.hot      { color: #fb923c; }
        .chip-value.critical { color: #f87171; }

        /* ── Responsive fallback ── */
        @media (max-width: 768px) {
            .main {
                grid-template-columns: 1fr;
                grid-template-rows: auto;
                height: auto;
            }
            .cam-panel { grid-row: auto; aspect-ratio: 16/9; }
            .right-col { flex-direction: row; flex-wrap: wrap; }
            .state-card, .metrics-card { flex: 1 1 240px; }
            .status-strip { grid-column: 1; grid-template-columns: repeat(2, 1fr); }
        }
    </style>
</head>
<body>
    <!-- ── Top bar ── -->
    <div class="topbar">
        <div class="topbar-left">
            <span class="logo">TransitGuard</span>
            <div class="live-badge">
                <div class="live-dot"></div>
                LIVE MONITOR
            </div>
        </div>
        <div class="topbar-right">
            <span class="device-label" id="last-updated">--</span>
            <a class="nav-temp" id="nav-temp" href="/temperature"
               aria-label="Pi temperature — open the temperature view">
                <span aria-hidden="true">🌡</span><span class="tval" id="nav-temp-val">--</span>
            </a>
            <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn"
                    aria-label="Switch to light theme">
                <span class="toggle-icon" id="toggle-icon" aria-hidden="true">☀</span>
                <span id="toggle-label">Light</span>
            </button>
        </div>
    </div>

    <!-- ── Main grid ── -->
    <div class="main">
        <!-- Camera panel -->
        <div class="cam-panel">
            <img src="/video_feed" alt="Live camera feed">
            <div class="cam-overlay">
                <div class="cam-label" id="cam-label">DRIVER CAM</div>
                <div class="cam-state-badge">
                    <div id="state-overlay" class="state-badge NORMAL" style="font-size:13px;padding:5px 12px;">NORMAL</div>
                </div>
            </div>
        </div>

        <!-- Right column -->
        <div class="right-col">
            <!-- Driver state card -->
            <div class="state-card">
                <div class="state-title">Driver State</div>
                <div id="state" class="state-badge NORMAL">NORMAL</div>
            </div>

            <!-- Live metrics card -->
            <div class="metrics-card">
                <div class="metrics-title">Live Metrics</div>

                <div class="metric-row">
                    <span class="metric-label">EAR</span>
                    <span class="metric-value" id="ear">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">MAR</span>
                    <span class="metric-value" id="mar">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">PERCLOS</span>
                    <span class="metric-value" id="perclos">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">Head Pitch</span>
                    <span class="metric-value" id="pitch">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">GPS</span>
                    <span class="metric-value accent" id="gps">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">Satellites</span>
                    <span class="metric-value muted" id="sats">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">MQ-3 Ratio</span>
                    <span class="metric-value" id="alcohol">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">LCD Msg</span>
                    <span class="metric-value muted" id="lcd">--</span>
                </div>
                <div class="metric-row">
                    <span class="metric-label">Pi Temp</span>
                    <span class="metric-value" id="pi-temp">--</span>
                </div>
            </div>

            <!-- Wi-Fi switch card (shown only when the feature is enabled) -->
            <div class="state-card" id="wifi-card" style="display:none">
                <div class="state-title">Network</div>
                <div class="wifi-current" id="wifi-current">--</div>
                <button class="wifi-btn" id="wifi-btn" onclick="switchWifi()">Switch</button>
                <div class="wifi-note" id="wifi-note"></div>
            </div>
        </div>

        <!-- Status strip -->
        <div class="status-strip">
            <div class="status-chip">
                <span class="chip-label">Network</span>
                <span class="chip-value" id="network" role="status"
                      aria-label="Network status unknown">--</span>
            </div>
            <div class="status-chip">
                <span class="chip-label">Offline Buffer</span>
                <span class="chip-value" id="buffer">--</span>
            </div>
            <div class="status-chip">
                <span class="chip-label">Buzzer</span>
                <span class="chip-value" id="buzzer" role="status"
                      aria-label="Buzzer status unknown">--</span>
            </div>
            <div class="status-chip">
                <span class="chip-label">Last Update</span>
                <span class="chip-value accent" id="updated">--</span>
            </div>
            <div class="status-chip">
                <span class="chip-label">Pi Temp</span>
                <span class="chip-value" id="temp-chip" role="status"
                      aria-label="Pi temperature unknown">--</span>
            </div>
        </div>
    </div>

    <script>
        /* ── Theme toggle ── */
        function applyTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            const icon  = document.getElementById('toggle-icon');
            const label = document.getElementById('toggle-label');
            const btn   = document.getElementById('theme-btn');
            if (theme === 'dark') {
                icon.textContent  = '\\u2600';  // ☀
                label.textContent = 'Light';
                btn.setAttribute('aria-label', 'Switch to light theme');
            } else {
                icon.textContent  = '\\u263D';  // ☽
                label.textContent = 'Dark';
                btn.setAttribute('aria-label', 'Switch to dark theme');
            }
            try { localStorage.setItem('tg_theme', theme); } catch(e) {}
        }
        function toggleTheme() {
            const cur = document.documentElement.getAttribute('data-theme');
            applyTheme(cur === 'dark' ? 'light' : 'dark');
        }
        // Restore saved preference on load
        (function() {
            try {
                const saved = localStorage.getItem('tg_theme');
                if (saved === 'light' || saved === 'dark') applyTheme(saved);
            } catch(e) {}
        })();

        /* Age of a stale position in a form a human can read at a glance:
           '45s', '2m 14s', '1h 3m', '3d 4h'. Mirrors _format_age() in the
           Python overlay. */
        function fmtAge(seconds) {
            if (seconds === null || seconds === undefined || isNaN(seconds)) return 'unknown';
            let s = Math.round(Math.max(0, seconds));
            if (s < 60) return s + 's';
            const m = Math.floor(s / 60); s = s % 60;
            if (m < 60) return m + 'm ' + s + 's';
            const h = Math.floor(m / 60), rm = m % 60;
            if (h < 24) return h + 'h ' + rm + 'm';
            const d = Math.floor(h / 24), rh = h % 24;
            return d + 'd ' + rh + 'h';
        }

        /* ── Data refresh ── */
        async function refresh() {
            let m;
            try {
                const res = await fetch('/data');
                m = await res.json();
            } catch(e) { return; }

            // State
            const stateEl = document.getElementById('state');
            const stateOv = document.getElementById('state-overlay');
            stateEl.textContent = m.driver_state;
            stateEl.className   = 'state-badge ' + m.driver_state;
            stateOv.textContent = m.driver_state;
            stateOv.className   = 'state-badge ' + m.driver_state;

            // Helper to set value + optional colour class
            function setVal(id, text, cls) {
                const el = document.getElementById(id);
                el.textContent = text;
                if (cls) {
                    el.className = 'metric-value ' + cls;
                }
            }

            setVal('ear',     m.ear     !== null ? m.ear.toFixed(3)              : '--');
            setVal('mar',     m.mar     !== null ? m.mar.toFixed(3)              : '--');
            setVal('perclos', m.perclos !== null ? (m.perclos*100).toFixed(1)+'%' : '--');
            setVal('pitch',   m.pitch   !== null ? m.pitch.toFixed(1)+'\\u00b0'   : '--');

            // GPS
            let gpsText = 'no fix yet';
            let gpsCls  = 'muted';
            if (m.fix_valid) {
                gpsText = m.latitude.toFixed(5) + ', ' + m.longitude.toFixed(5);
                gpsCls  = 'accent';
            } else if (m.using_last_known && m.latitude !== null) {
                gpsText = m.latitude.toFixed(5) + ', ' + m.longitude.toFixed(5)
                        + ' (stale ' + fmtAge(m.location_age_sec) + ')';
                gpsCls  = 'warn';
            }
            setVal('gps', gpsText, gpsCls);

            // Satellites — GGA reports those used in the fix, GSV those in
            // view; a module may emit only some of these sentences.
            let satsText = '--';
            let satsCls  = 'muted';
            if (m.sats_used !== null || m.sats_in_view !== null) {
                const used = m.sats_used   !== null ? m.sats_used   : '?';
                const view = m.sats_in_view !== null ? m.sats_in_view : '?';
                satsText = used + ' used / ' + view + ' in view'
                         + (m.best_snr !== null ? ' | best ' + m.best_snr + ' dBHz' : '');
                satsCls  = (m.sats_used && m.sats_used >= 4) ? 'accent' : 'warn';
            } else {
                satsText = 'not reported by module';
            }
            setVal('sats', satsText, satsCls);

            // Alcohol - the MQ-3 ratio FALLS when alcohol is present (clean air
            // reads ~5.x, a real alcohol response near the sensor reads ~0.9), so
            // the comparison must be '<', not '>'. Thresholds come from the device
            // so this cannot drift from the app's own alert rule. The class is
            // always reset so the colour cannot get stuck.
            const alcEl = document.getElementById('alcohol');
            alcEl.textContent = m.alcohol_ratio !== null ? m.alcohol_ratio.toFixed(2) : '--';
            const alcAlertThr   = m.alcohol_alert_threshold   ?? 2.5;
            const alcCautionThr = m.alcohol_caution_threshold ?? 4.0;
            let alcCls = '';
            if (m.alcohol_ratio !== null) {
                if (m.alcohol_ratio < alcAlertThr)        alcCls = 'alert';
                else if (m.alcohol_ratio < alcCautionThr) alcCls = 'warn';
            }
            alcEl.className = 'metric-value' + (alcCls ? ' ' + alcCls : '');

            // LCD
            setVal('lcd', m.lcd_message || '--');

            // Pi SoC temperature — same levels/colours as the /temperature view.
            // Colour is never the only signal: the value text and the aria-labels
            // carry the same information.
            const piTemp = m.pi_temperature;
            const piLevel = m.pi_temp_level || null;
            const piTempText = (piTemp === null || piTemp === undefined)
                ? '--' : piTemp.toFixed(1) + '°C';
            const piTempCls = piLevel ? ' ' + piLevel : '';

            const piTempEl = document.getElementById('pi-temp');
            piTempEl.textContent = piTempText;
            piTempEl.className = 'metric-value' + piTempCls;

            const tempChip = document.getElementById('temp-chip');
            tempChip.textContent = piTempText;
            tempChip.className = 'chip-value' + piTempCls;
            tempChip.setAttribute('aria-label', (piTemp === null || piTemp === undefined)
                ? 'Pi temperature unavailable'
                : 'Pi temperature ' + piTempText + (piLevel ? ', ' + piLevel : ''));

            const navTemp = document.getElementById('nav-temp');
            if (navTemp) {
                navTemp.className = 'nav-temp' + piTempCls;
                const navTempVal = document.getElementById('nav-temp-val');
                if (navTempVal) navTempVal.textContent = piTempText;
                navTemp.setAttribute('aria-label', 'Pi temperature ' + piTempText +
                    ' — open the temperature view');
            }

            // Status strip — colours are backed by text and by aria-labels,
            // so no state is carried by colour alone.
            const netEl = document.getElementById('network');
            netEl.textContent = m.network_online ? 'Online (WiFi)' : 'Offline (LoRa)';
            netEl.className   = 'chip-value ' + (m.network_online ? 'online' : 'offline');
            netEl.setAttribute('aria-label', m.network_online
                ? 'Network online, using WiFi'
                : 'Network offline, using LoRa fallback');

            document.getElementById('buffer').textContent = m.buffer_count + ' pending';

            const buzEl = document.getElementById('buzzer');
            buzEl.textContent = m.buzzer_active ? 'ON' : 'off';
            buzEl.className   = 'chip-value ' + (m.buzzer_active ? 'on' : 'off');
            buzEl.setAttribute('aria-label', m.buzzer_active ? 'Buzzer on' : 'Buzzer off');

            document.getElementById('updated').textContent = m.updated_at || '--';
            document.getElementById('last-updated').textContent = m.updated_at ? ('Updated ' + m.updated_at) : '';

            // Camera label reflects the resolution the device is really sending,
            // so the page can never advertise a resolution the feed does not have.
            const camLabel = document.getElementById('cam-label');
            if (camLabel && m.frame_width && m.frame_height) {
                camLabel.textContent = 'DRIVER CAM · ' + m.frame_width + '×' + m.frame_height;
            }
        }

        /* ── Wi-Fi switch ── */
        async function refreshWifi() {
            let s;
            try {
                const res = await fetch('/wifi/status');
                s = await res.json();
            } catch (e) { return; }

            const card = document.getElementById('wifi-card');
            const cur  = document.getElementById('wifi-current');
            const btn  = document.getElementById('wifi-btn');
            if (!s.enabled) { card.style.display = 'none'; return; }
            card.style.display = '';

            cur.textContent = s.current_ssid
                ? ('Connected: ' + s.current_ssid + (s.current_ip ? ' (' + s.current_ip + ')' : ''))
                : 'Not connected';
            if (s.next_ssid && !btn.dataset.busy) {
                btn.style.display = '';
                btn.disabled = false;
                btn.textContent = 'Switch to ' + s.next_ssid;
            } else if (!s.next_ssid) {
                btn.style.display = 'none';
            }
        }

        async function switchWifi() {
            const btn  = document.getElementById('wifi-btn');
            const note = document.getElementById('wifi-note');
            const proceed = confirm('Switch the Pi to a different Wi-Fi network? ' +
                'This dashboard disconnects immediately. Reconnect this device ' +
                'to the new network (then use john.local) to reach it again.');
            if (!proceed) { return; }

            btn.dataset.busy = '1';
            btn.disabled = true;
            btn.textContent = 'Switching...';
            note.textContent = 'Switching network - this page will lose connection.';
            try {
                const res = await fetch('/wifi/switch', { method: 'POST' });
                const r = await res.json();
                if (r.ok) {
                    note.textContent = 'Switching to ' + r.switching_to +
                        '. Rejoin that network, then reload john.local:8080.';
                } else {
                    btn.disabled = false;
                    delete btn.dataset.busy;
                    btn.textContent = 'Retry';
                    note.textContent = 'Failed: ' + (r.error || 'unknown error');
                }
            } catch (e) {
                note.textContent = 'Switching - rejoin the target network to continue.';
            }
        }

        setInterval(refreshWifi, 5000);
        refreshWifi();

        setInterval(refresh, 1000);
        refresh();
    </script>
</body>
</html>
"""



TEMPERATURE_HTML = """
<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
    <title>TransitGuard — Pi Temperature</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta http-equiv="refresh" content="30">
    <style>
        :root[data-theme="dark"] {
            --bg:#111214; --panel:#1a1c1f; --border:#2e3035;
            --text:#e8eaed; --muted:#7a7f8a; --accent:#4fc3f7;
        }
        :root[data-theme="light"] {
            --bg:#e8eaed; --panel:#f5f6f8; --border:#d1d4da;
            --text:#1a1c20; --muted:#6b7280; --accent:#0284c7;
        }
        * { box-sizing:border-box; margin:0; padding:0; }
        body {
            font-family:-apple-system,'Segoe UI',sans-serif;
            background:var(--bg); color:var(--text);
            min-height:100vh; display:flex; flex-direction:column;
            align-items:center; justify-content:center; padding:24px;
        }
        .back {
            position:absolute; top:16px; left:20px;
            color:var(--accent); text-decoration:none; font-size:13px; font-weight:600;
        }
        .back:hover { text-decoration:underline; }
        .theme-toggle {
            position:absolute; top:12px; right:20px;
            background:var(--panel); border:1px solid var(--border); border-radius:20px;
            padding:5px 14px; cursor:pointer; font-size:13px; color:var(--text);
        }
        .card {
            background:var(--panel); border:1px solid var(--border);
            border-radius:16px; padding:36px 44px; text-align:center;
            max-width:520px; width:100%;
        }
        .label {
            font-size:11px; font-weight:700; letter-spacing:2px;
            text-transform:uppercase; color:var(--muted); margin-bottom:14px;
        }
        .temp {
            font-size:88px; font-weight:800; line-height:1;
            font-variant-numeric:tabular-nums; letter-spacing:-2px;
        }
        .temp.cool     { color:#4ade80; }
        .temp.warm     { color:#fbbf24; }
        .temp.hot      { color:#fb923c; }
        .temp.critical { color:#f87171; }
        .level {
            display:inline-block; margin-top:14px; padding:6px 16px;
            border-radius:20px; font-size:13px; font-weight:800; letter-spacing:1.5px;
            border:1px solid var(--border);
        }
        .level.cool     { color:#4ade80; background:rgba(74,222,128,.12); }
        .level.warm     { color:#fbbf24; background:rgba(251,191,36,.12); }
        .level.hot      { color:#fb923c; background:rgba(251,146,60,.12); }
        .level.critical { color:#f87171; background:rgba(248,113,113,.14); }
        .note { margin-top:16px; font-size:12px; color:var(--muted); min-height:16px; }
        .legend {
            display:flex; flex-wrap:wrap; gap:8px 18px; justify-content:center;
            margin-top:22px; padding-top:18px; border-top:1px solid var(--border);
            font-size:11px; color:var(--muted);
        }
        .legend span { display:flex; align-items:center; gap:6px; }
        .dot { width:9px; height:9px; border-radius:50%; display:inline-block; }
        .dot.cool { background:#4ade80; } .dot.warm { background:#fbbf24; }
        .dot.hot { background:#fb923c; } .dot.critical { background:#f87171; }
        .meta {
            display:flex; justify-content:space-between; gap:12px;
            margin-top:18px; font-size:11px; color:var(--muted);
            font-variant-numeric:tabular-nums;
        }
        .hot-banner {
            display:none; margin-bottom:16px; padding:10px 14px; border-radius:10px;
            background:rgba(248,113,113,.15); border:1px solid rgba(248,113,113,.4);
            color:#f87171; font-size:13px; font-weight:700;
        }
        .hot-banner.show { display:block; }
        @media (max-width:520px){ .temp{font-size:64px;} .card{padding:28px 22px;} }
    </style>
</head>
<body>
    <a class="back" href="/">&larr; Back to dashboard</a>
    <button class="theme-toggle" onclick="toggleTheme()" id="theme-btn"
            aria-label="Switch to light theme">Light</button>

    <div class="card">
        <div class="hot-banner" id="hot-banner">Pi is HOT &mdash; check ventilation / load</div>
        <div class="label">Raspberry Pi SoC Temperature</div>
        <div class="temp" id="temp">--</div>
        <div><span class="level" id="level">--</span></div>
        <div class="note" id="note">--</div>
        <div class="legend">
            <span><i class="dot cool"></i>&lt; 70 &deg;C &middot; cool</span>
            <span><i class="dot warm"></i>70&ndash;80 &deg;C &middot; warm</span>
            <span><i class="dot hot"></i>80&ndash;85 &deg;C &middot; hot</span>
            <span><i class="dot critical"></i>&ge; 85 &deg;C &middot; critical</span>
        </div>
        <div class="meta">
            <span id="throttled">throttled --</span>
            <span id="updated">updated --</span>
        </div>
    </div>

    <script>
        const LEVELS = {
            cool:     ['COOL',     '#4ade80'],
            warm:     ['WARM',     '#fbbf24'],
            hot:      ['HOT',      '#fb923c'],
            critical: ['CRITICAL', '#f87171'],
        };
        function applyTheme(theme) {
            document.documentElement.setAttribute('data-theme', theme);
            const btn = document.getElementById('theme-btn');
            btn.textContent = theme === 'dark' ? 'Light' : 'Dark';
            btn.setAttribute('aria-label', theme === 'dark'
                ? 'Switch to light theme' : 'Switch to dark theme');
            try { localStorage.setItem('tg_theme', theme); } catch(e) {}
        }
        function toggleTheme() {
            applyTheme(document.documentElement.getAttribute('data-theme') === 'dark'
                ? 'light' : 'dark');
        }
        try {
            const saved = localStorage.getItem('tg_theme');
            if (saved === 'light' || saved === 'dark') applyTheme(saved);
        } catch(e) {}

        async function tick() {
            let d;
            try {
                const r = await fetch('/temperature/data');
                d = await r.json();
            } catch (e) { return; }

            const t = document.getElementById('temp');
            const lv = document.getElementById('level');
            if (d.temperature === null) {
                t.textContent = '--';
                t.className = 'temp';
                lv.textContent = 'unavailable';
                lv.className = 'level';
            } else {
                t.textContent = d.temperature.toFixed(1) + ' °C';
                const [label, color] = LEVELS[d.level] || ['--', '#7a7f8a'];
                t.className = 'temp ' + (d.level || '');
                t.style.color = color;
                lv.textContent = label;
                lv.className = 'level ' + (d.level || '');
                lv.style.color = color;
            }
            document.getElementById('note').textContent
                = d.note || 'no active throttling flags';
            document.getElementById('throttled').textContent
                = d.throttled ? ('throttled ' + d.throttled) : 'throttled n/a';
            document.getElementById('updated').textContent
                = 'updated ' + (d.updated_at || '--');
            document.getElementById('hot-banner').className
                = 'hot-banner' + (d.level === 'hot' || d.level === 'critical' ? ' show' : '');
        }

        setInterval(tick, 2000);
        tick();
    </script>
</body>
</html>
"""


@dashboard_app.route("/")
def dashboard_index():
    return render_template_string(DASHBOARD_HTML)


@dashboard_app.route("/video_feed")
def dashboard_video_feed():
    return Response(_mjpeg_generator(), mimetype="multipart/x-mixed-replace; boundary=frame")


@dashboard_app.route("/data")
def dashboard_data():
    with dashboard_lock:
        payload = dict(latest_metrics)
    # Expose the alcohol thresholds so the dashboard's colouring cannot drift
    # from the app's own alert rule.
    payload["alcohol_alert_threshold"] = MQ3_ALERT_RATIO_THRESHOLD
    payload["alcohol_caution_threshold"] = MQ3_CAUTION_RATIO_THRESHOLD
    # SoC temperature / throttling, read fresh (sysfs, cached 1 s).
    temp, throttled, note = read_pi_thermal()
    payload["pi_temperature"] = temp
    payload["pi_throttled"] = throttled
    payload["pi_thermal_note"] = note
    payload["pi_temp_level"] = pi_temp_level(temp)
    return jsonify(payload)


@dashboard_app.route("/temperature")
@dashboard_app.route("/temp")
def dashboard_temperature():
    """Dedicated at-a-glance Pi temperature page."""
    return render_template_string(TEMPERATURE_HTML)


@dashboard_app.route("/temperature/data")
def dashboard_temperature_data():
    """JSON feed for the temperature page (polled every 2 s)."""
    temp, throttled, note = read_pi_thermal()
    return jsonify({
        "temperature": temp,
        "level": pi_temp_level(temp),
        "throttled": throttled,
        "note": note,
        "updated_at": time.strftime("%H:%M:%S"),
    })


@dashboard_app.route("/wifi/status")
def dashboard_wifi_status():
    """Report the current SSID and which network the switch button would go to."""
    current = get_current_ssid()
    target = WIFI_SWITCH_TARGET_SSID or None
    home = WIFI_SWITCH_HOME_SSID or None
    if current and target and current == target:
        next_ssid = home          # already on the secondary network -> offer to go home
    else:
        next_ssid = target        # default -> offer the secondary network
    return jsonify({
        "enabled": WIFI_SWITCH_ENABLED,
        "current_ssid": current,
        "current_ip": get_ip_address(),
        "target_ssid": target,
        "home_ssid": home,
        "next_ssid": next_ssid,
    })


@dashboard_app.route("/wifi/switch", methods=["POST"])
def dashboard_wifi_switch():
    """Toggle between the home and secondary networks. Responds immediately,
    then performs the switch ~2 s later in a detached process, because the
    Pi's Wi-Fi change severs this very HTTP connection."""
    if not WIFI_SWITCH_ENABLED:
        return jsonify({"ok": False, "error": "Wi-Fi switch is not enabled"}), 403

    current = get_current_ssid()
    if current and WIFI_SWITCH_TARGET_SSID and current == WIFI_SWITCH_TARGET_SSID:
        dest_ssid, going_to_target = WIFI_SWITCH_HOME_SSID, False
    else:
        dest_ssid, going_to_target = WIFI_SWITCH_TARGET_SSID, True

    if not dest_ssid:
        return jsonify({"ok": False, "error": "destination SSID is not configured"}), 400

    if going_to_target:
        ok, info = ensure_switch_profile()
        if not ok:
            return jsonify({"ok": False, "error": info}), 500
        profile_name = info
    else:
        profile_name = _find_connection_by_ssid(dest_ssid)
        if not profile_name:
            return jsonify({"ok": False, "error": f"no saved profile for {dest_ssid!r}"}), 500

    if not schedule_wifi_switch(profile_name):
        return jsonify({"ok": False, "error": "could not schedule the switch"}), 500

    return jsonify({
        "ok": True,
        "switching_to": dest_ssid,
        "profile": profile_name,
        "note": f"Reconnect this device to '{dest_ssid}' to reach the dashboard again.",
    })


def dashboard_server_thread():
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)  # quiet the request-log spam
    dashboard_app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, threaded=True, use_reloader=False)


# ---------------------------
# MEDIAPIPE TASKS API SETUP
# ---------------------------
base_options = mp_python.BaseOptions(model_asset_path=MODEL_PATH)
options = mp_vision.FaceLandmarkerOptions(
    base_options=base_options,
    running_mode=mp_vision.RunningMode.VIDEO,
    num_faces=1,
    min_face_detection_confidence=0.5,
    min_tracking_confidence=0.5,
)
landmarker = mp_vision.FaceLandmarker.create_from_options(options)


# ---------------------------
# HELPER FUNCTIONS
# ---------------------------
def euclidean(p1, p2):
    return np.linalg.norm(np.array(p1) - np.array(p2))


def aspect_ratio(landmarks, corners, vertical_pairs, w, h):
    """
    Generalized eye/mouth aspect ratio: averages THREE vertical
    measurements (sampled at different horizontal positions across the
    eye or mouth) instead of just two. This smooths out per-frame
    landmark jitter and noise from any single point, giving a more
    stable and accurate open/closed signal than the classic 2-point EAR.
    """
    c1 = (landmarks[corners[0]].x * w, landmarks[corners[0]].y * h)
    c2 = (landmarks[corners[1]].x * w, landmarks[corners[1]].y * h)
    horizontal = euclidean(c1, c2)
    if horizontal == 0:
        return 0

    vertical_sum = 0.0
    for a, b in vertical_pairs:
        pa = (landmarks[a].x * w, landmarks[a].y * h)
        pb = (landmarks[b].x * w, landmarks[b].y * h)
        vertical_sum += euclidean(pa, pb)

    avg_vertical = vertical_sum / len(vertical_pairs)
    return avg_vertical / horizontal


def get_head_pose(landmarks, w, h):
    face_2d = np.array(
        [(landmarks[i].x * w, landmarks[i].y * h) for i in FACE_2D_INDICES],
        dtype=np.float64
    )
    focal_length = w
    cam_matrix = np.array([
        [focal_length, 0, w / 2],
        [0, focal_length, h / 2],
        [0, 0, 1]
    ], dtype=np.float64)
    dist_coeffs = np.zeros((4, 1))

    success, rotation_vec, _ = cv2.solvePnP(
        FACE_3D_MODEL, face_2d, cam_matrix, dist_coeffs
    )
    if not success:
        return None, None, None

    rmat, _ = cv2.Rodrigues(rotation_vec)
    sy = np.sqrt(rmat[0, 0] ** 2 + rmat[1, 0] ** 2)
    singular = sy < 1e-6

    if not singular:
        pitch = np.arctan2(-rmat[2, 0], sy)
        yaw = np.arctan2(rmat[1, 0], rmat[0, 0])
        roll = np.arctan2(rmat[2, 1], rmat[2, 2])
    else:
        pitch = np.arctan2(-rmat[2, 0], sy)
        yaw = 0
        roll = np.arctan2(-rmat[1, 2], rmat[1, 1])

    return np.degrees(pitch), np.degrees(yaw), np.degrees(roll)


def init_log_file():
    with open(LOG_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp", "ear", "mar", "perclos", "pitch", "driver_state",
            "latitude", "longitude", "fix_valid", "using_last_known",
            "location_age_sec", "mq3_ratio", "mq3_ppm",
            "supabase_sent", "buffer_count"
        ])


def log_event(ear, mar, perclos, pitch, driver_state, location, alcohol, sent, buf_count):
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            f"{ear:.3f}" if ear is not None else "",
            f"{mar:.3f}" if mar is not None else "",
            f"{perclos:.3f}" if perclos is not None else "",
            f"{pitch:.1f}" if pitch is not None else "",
            driver_state,
            location["latitude"] if location["latitude"] is not None else "",
            location["longitude"] if location["longitude"] is not None else "",
            location["fix_valid"],
            location.get("using_last_known", False),
            f"{location['location_age_sec']:.1f}" if location.get("location_age_sec") is not None else "",
            f"{alcohol['ratio']:.3f}" if alcohol["ratio"] is not None else "",
            f"{alcohol['ppm']:.2f}" if alcohol["ppm"] is not None else "",
            sent,
            buf_count,
        ])


# ---------------------------
# MAIN LOOP
# ---------------------------
stop_event = threading.Event()


def _handle_shutdown_signal(signum, frame):
    print(f"Received signal {signum}, shutting down gracefully...")
    stop_event.set()


def main():
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)

    init_buffer_db()
    init_log_file()

    gps_thread = threading.Thread(target=gps_reader_thread, daemon=True)
    gps_thread.start()

    alcohol_thread = threading.Thread(target=alcohol_reader_thread, daemon=True)
    alcohol_thread.start()

    lora_ser = setup_lora()
    last_lora_heartbeat = 0
    last_supabase_send = 0
    last_published_state = None   # driver state of the last telemetry we queued
    alert_coalesced = 0           # alert sends skipped by the rate limit (diagnostic)

    uploader_thread = threading.Thread(target=telemetry_uploader_thread, daemon=True)
    uploader_thread.start()

    lora_thread = threading.Thread(target=lora_sender_thread, args=(lora_ser,), daemon=True)
    lora_thread.start()

    lcd = setup_lcd()
    buzzer_ready = setup_buzzer()
    if lcd is not None:
        time.sleep(LCD_BOOT_IP_DISPLAY_SEC)  # keep the IP address visible before it's overwritten
    act_thread = threading.Thread(target=actuator_thread, args=(lcd, buzzer_ready), daemon=True)
    act_thread.start()
    last_actuator_state = None

    if ENABLE_DASHBOARD:
        dash_thread = threading.Thread(target=dashboard_server_thread, daemon=True)
        dash_thread.start()
        print(f"Live dashboard running at http://{get_ip_address()}:{DASHBOARD_PORT}")

    # Optionally lower resolution here (e.g. width=320, height=240) if you
    # want to trade a bit of detection precision for more speed on the Pi 4.
    cam = ThreadedCamera(src=0)
    if not cam.ok:
        print("Could not open camera.")
        return

    eye_closed_frames = 0
    perclos_history = deque()
    start_time = time.time()
    last_log_time = 0
    LOG_INTERVAL_SEC = 1

    while not stop_event.is_set():
        ret, frame = cam.read()
        if not ret:
            time.sleep(0.01)  # camera thread hasn't produced a frame yet
            continue

        h, w, _ = frame.shape
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)

        timestamp_ms = int((time.time() - start_time) * 1000)
        result = landmarker.detect_for_video(mp_image, timestamp_ms)

        status_text = "No face detected"
        avg_ear = mar = perclos = pitch = yaw = roll = None

        location = get_current_location()
        alcohol = get_current_alcohol()

        if result.face_landmarks:
            landmarks = result.face_landmarks[0]

            # Compute head pose FIRST — EAR/MAR are only geometrically valid
            # when the face is reasonably frontal. At an angle, perspective
            # foreshortens the eye/mouth landmarks and falsely lowers EAR,
            # even with eyes wide open. So we check pose before trusting them.
            pitch, yaw, roll = get_head_pose(landmarks, w, h)

            is_frontal = (
                yaw is not None
                and abs(yaw) <= FACE_YAW_LIMIT_FOR_EAR
                and abs(pitch) <= FACE_PITCH_LIMIT_FOR_EAR
            )

            if is_frontal:
                left_ear = aspect_ratio(landmarks, LEFT_EYE_CORNERS, LEFT_EYE_VERTICAL_PAIRS, w, h)
                right_ear = aspect_ratio(landmarks, RIGHT_EYE_CORNERS, RIGHT_EYE_VERTICAL_PAIRS, w, h)
                avg_ear = (left_ear + right_ear) / 2.0

                eyes_closed_now = avg_ear < EAR_THRESHOLD
                eye_closed_frames = eye_closed_frames + 1 if eyes_closed_now else 0

                now = time.time()
                perclos_history.append((now, eyes_closed_now))
                while perclos_history and now - perclos_history[0][0] > PERCLOS_WINDOW_SEC:
                    perclos_history.popleft()

                perclos = 0.0
                if perclos_history:
                    closed_count = sum(1 for _, closed in perclos_history if closed)
                    perclos = closed_count / len(perclos_history)

                mar = aspect_ratio(landmarks, MOUTH_CORNERS, MOUTH_VERTICAL_PAIRS, w, h)

                status_text = f"EAR:{avg_ear:.2f} MAR:{mar:.2f} PERCLOS:{perclos:.2f}"
            else:
                # Face detected but turned too far away — freeze EAR/PERCLOS
                # updates rather than feeding in unreliable readings. Head
                # pose itself (pitch) is still valid and still checked for
                # nodding-off below, since that's not an EAR-based signal.
                perclos = (
                    sum(1 for _, closed in perclos_history if closed) / len(perclos_history)
                    if perclos_history else 0.0
                )
                status_text = "Face not frontal — repositioning needed for EAR"

            if pitch is not None:
                status_text += f" Pitch:{pitch:.1f}"

            for idx in LEFT_EYE + RIGHT_EYE + MOUTH:
                x, y = int(landmarks[idx].x * w), int(landmarks[idx].y * h)
                cv2.circle(frame, (x, y), 2, (0, 255, 0), -1)
        else:
            eye_closed_frames = 0

        driver_state = classify_driver_state(
            avg_ear, eye_closed_frames, perclos, pitch, alcohol["ratio"]
        )
        # Test-only scripted override (see read_test_state). None unless the
        # evaluation harness explicitly enabled it.
        test_state = read_test_state()
        if test_state:
            driver_state = test_state
        alert_active = driver_state != "NORMAL"

        if driver_state != last_actuator_state:
            queue_put_latest(actuator_queue, driver_state)
            last_actuator_state = driver_state

        # --- Overlays ---
        if location["fix_valid"]:
            loc_text = f"GPS: {location['latitude']:.5f}, {location['longitude']:.5f}"
        elif location.get("using_last_known"):
            loc_text = (f"GPS(stale {_format_age(location.get('location_age_sec'))}): "
                        f"{location['latitude']:.5f}, {location['longitude']:.5f}")
        else:
            loc_text = "GPS: no fix yet"
        alc_text = (f"MQ-3 ratio: {alcohol['ratio']:.2f}"
                    if alcohol["ratio"] is not None else "MQ-3: no reading yet")

        color = (0, 0, 255) if alert_active else (0, 255, 0)

        # HUD text scales with the frame so it stays legible and never runs off
        # the edge at any camera resolution, and is drawn over a dark outline so
        # it stays readable on bright frames.
        hud_scale = max(0.5, w / 640.0)
        hud_x = int(round(10 * hud_scale))
        hud_thick = max(1, int(round(2 * hud_scale)))
        _put_hud_text(frame, status_text, (hud_x, int(round(30 * hud_scale))),
                      0.6 * hud_scale, color, hud_thick)
        _put_hud_text(frame, f"State: {driver_state}", (hud_x, int(round(62 * hud_scale))),
                      0.6 * hud_scale, color, hud_thick)
        _put_hud_text(frame, loc_text, (hud_x, int(round(94 * hud_scale))),
                      0.5 * hud_scale, (255, 255, 0), max(1, int(round(1 * hud_scale))))
        _put_hud_text(frame, alc_text, (hud_x, int(round(116 * hud_scale))),
                      0.5 * hud_scale, (255, 255, 0), max(1, int(round(1 * hud_scale))))

        # --- LoRa (non-blocking) ---
        # Design decision: critical safety alerts (DROWSY/CRITICAL_FATIGUE/
        # ALCOHOL_ALERT) are always relayed over LoRa immediately, regardless
        # of whether the primary link is up — redundancy for safety-critical
        # events. Routine telemetry is only sent over LoRa when the primary
        # (WiFi/cellular -> Supabase) path is actually down, which is the
        # true "fallback" behavior your thesis objective 3 describes.
        if alert_active:
            queue_put_latest(lora_queue, build_lora_packet(avg_ear, mar, perclos, pitch,
                                                            driver_state, location, alcohol))
            last_lora_heartbeat = time.time()
        elif (not is_network_online()) and (time.time() - last_lora_heartbeat >= LORA_HEARTBEAT_INTERVAL_SEC):
            queue_put_latest(lora_queue, build_lora_packet(avg_ear, mar, perclos, pitch,
                                                            driver_state, location, alcohol))
            last_lora_heartbeat = time.time()

        # --- Supabase: publish on state change or at the configured cadence ---
        # The gap required before the next send is the normal cadence, tightened
        # while an alert is active or a transition has just occurred. This
        # reports alert onset promptly (after a quiet NORMAL stretch the elapsed
        # gap already exceeds the shorter floor) without letting the ~20 FPS
        # loop enqueue far faster than the uploader drains — which previously
        # caused drop-oldest to discard most alert messages (run 3).
        sent = None
        now_ts = time.time()
        state_changed = driver_state != last_published_state
        min_gap = (ALERT_PUBLISH_MIN_INTERVAL_SEC
                   if (alert_active or state_changed) else PUBLISH_INTERVAL_SEC)
        should_send_now = (now_ts - last_supabase_send) >= min_gap
        if not should_send_now and alert_active:
            alert_coalesced += 1
        if should_send_now:
            payload = build_telemetry_payload(
                avg_ear, mar, perclos, pitch, yaw, roll, location, alcohol, driver_state,
                transport_mode=("WIFI" if is_network_online() else "LORA"),
                test_state=test_state,
            )
            queue_put_latest(telemetry_queue, payload, trace_label="telemetry")
            last_supabase_send = now_ts
            last_published_state = driver_state
            sent = "queued"  # actual success/failure is logged by the uploader thread
            trace_event(
                ev="scheduled",
                messageId=payload.get("messageId"),
                device_ts_ms=payload.get("timestamp"),
                trigger=("alert" if alert_active else "interval"),
                driver_state=driver_state,
                queue_size=telemetry_queue.qsize(),
                transport_mode=("WIFI" if is_network_online() else "LORA"),
                state_changed=state_changed,
                alert_coalesced=alert_coalesced,
                test_injected_state=test_state,
            )

        # --- CSV logging (always, for evaluation data) ---
        if time.time() - last_log_time >= LOG_INTERVAL_SEC:
            log_event(avg_ear, mar, perclos, pitch, driver_state, location,
                      alcohol, sent, buffer_count())
            last_log_time = time.time()

        # --- Live browser dashboard (video feed + metrics, non-blocking) ---
        if ENABLE_DASHBOARD:
            update_dashboard_frame(frame)
            update_dashboard_metrics(
                driver_state=driver_state,
                ear=avg_ear, mar=mar, perclos=perclos, pitch=pitch,
                latitude=location["latitude"], longitude=location["longitude"],
                fix_valid=location["fix_valid"],
                using_last_known=location.get("using_last_known", False),
                location_age_sec=location.get("location_age_sec"),
                sats_used=location.get("sats_used"),
                sats_in_view=location.get("sats_in_view"),
                best_snr=location.get("best_snr"),
                fix_quality=location.get("fix_quality"),
                alcohol_ratio=alcohol["ratio"], alcohol_ppm=alcohol["ppm"],
                network_online=is_network_online(), buffer_count=buffer_count(),
                telemetry_queue_size=telemetry_queue.qsize(),
                alert_coalesced=alert_coalesced,
                buzzer_active=get_current_actuator()["buzzer_active"],
                lcd_message=get_current_actuator()["lcd_message"],
                test_injected_state=test_state,
            )

        if ENABLE_PREVIEW:
            cv2.imshow("Vehicle Monitor", frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    cam.release()
    if ENABLE_PREVIEW:
        cv2.destroyAllWindows()
    landmarker.close()
    if lora_ser is not None:
        lora_ser.close()
    set_buzzer(False)
    if lcd is not None:
        try:
            lcd.clear()
        except Exception:
            pass
    GPIO.cleanup()


# ---------------------------
# CLI DIAGNOSTIC MODES (parity with the integration guide's rpi_publisher.py)
# ---------------------------
def run_once():
    """Sends a single diagnostic packet with placeholder sensor values,
    to confirm authorization and connectivity without needing the camera
    or other hardware running."""
    init_buffer_db()
    print("Initializing TransitGuard diagnostic packet...")
    print(f"Target URL : {SUPABASE_INGEST_URL}")
    print(f"Device ID  : {DEVICE_ID}")
    masked = (DEVICE_TOKEN[:8] + "***") if DEVICE_TOKEN else "(not set)"
    print(f"Auth Token : {masked}")

    dummy_location = {"latitude": 9.582415, "longitude": 6.545892,
                       "speed_knots": None, "fix_valid": True}
    dummy_alcohol = {"voltage": 0.38, "ratio": 1.0, "ppm": 0.1}

    payload = build_telemetry_payload(
        0.325, 0.295, 0.038, 2.0, 0.0, 0.0, dummy_location, dummy_alcohol, "NORMAL"
    )
    if send_telemetry(payload):
        print(f"[LIVE SENT] {DEVICE_ID} | MsgId: {payload['messageId']}")
        print("Single diagnostic packet transmitted successfully.")
    else:
        print("Diagnostic packet could not be sent — check network/token. "
              "It has been queued in the local offline buffer.")


def run_status():
    init_buffer_db()
    print(f"Offline buffer: {buffer_count()} packet(s) pending at {BUFFER_DB_PATH}")


def run_flush():
    init_buffer_db()
    before = buffer_count()
    sent = flush_buffer(max_per_cycle=before or 1)
    print(f"Flushed {sent} of {before} buffered packet(s). "
          f"{buffer_count()} still pending.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TransitGuard vehicle monitor")
    parser.add_argument("--once", action="store_true", help="Send one diagnostic packet and exit")
    parser.add_argument("--status", action="store_true", help="Print offline buffer count and exit")
    parser.add_argument("--flush", action="store_true", help="Force-flush the offline buffer and exit")
    args = parser.parse_args()

    if args.once:
        run_once()
    elif args.status:
        run_status()
    elif args.flush:
        run_flush()
    else:
        main()
