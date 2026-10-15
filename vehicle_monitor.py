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
import os
import sqlite3
import sys
import uuid
import argparse
import threading
import queue
import signal
import subprocess
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

MQ3_RL_VALUE = 10.0
MQ3_R0 = 6.78  # PLACEHOLDER — replace with your calibrated R0
MQ3_VCC = 5.0
MQ3_CURVE_A = 0.4
MQ3_CURVE_B = -1.5
MQ3_ALERT_RATIO_THRESHOLD = 0.4    # ALCOHOL_ALERT threshold
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
        if _post_telemetry(payload):
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
def _post_telemetry(payload):
    """Raw HTTPS POST to the ingest-telemetry Edge Function. Returns True
    on HTTP 201, False on any failure (network, timeout, 4xx, 5xx)."""
    headers = {
        "Content-Type": "application/json",
        "x-device-token": DEVICE_TOKEN,
        "Authorization": f"Bearer {DEVICE_TOKEN}",
    }
    try:
        resp = requests.post(
            SUPABASE_INGEST_URL, json=payload, headers=headers, timeout=HTTP_TIMEOUT_SEC
        )
        if resp.status_code == 201:
            return True
        else:
            print(f"Supabase upload rejected: HTTP {resp.status_code} - {resp.text[:200]}")
            return False
    except requests.RequestException as e:
        print(f"Supabase upload failed (network): {e}")
        return False


def send_telemetry(payload):
    """
    Tries to flush any existing backlog first (so ordering stays mostly
    correct), then sends the current packet. On failure, the current
    packet is enqueued into the offline buffer instead of being lost.
    """
    if buffer_count() > 0:
        flush_buffer()

    if _post_telemetry(payload):
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
    """Returns (ssid, rssi_dbm) for wlan0, or (None, None) if unavailable."""
    try:
        ssid = subprocess.check_output(
            ["iwgetid", "-r"], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip() or None
    except Exception:
        ssid = None

    rssi = None
    try:
        output = subprocess.check_output(
            ["iwconfig", "wlan0"], stderr=subprocess.DEVNULL, timeout=2
        ).decode()
        for token in output.split():
            if token.startswith("level="):
                rssi = int(token.replace("level=", "").split("dBm")[0])
    except Exception:
        pass

    return ssid, rssi


def build_telemetry_payload(ear, mar, perclos, pitch, yaw, roll, location, alcohol,
                             driver_state, transport_mode="WIFI"):
    """Builds the full canonical JSON payload matching Section 6.2 of the
    integration guide, field-for-field."""
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
            "satelliteCount": None,
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
    return payload


def _knots_to_kmh(knots):
    if knots is None:
        return None
    return round(knots * 1.852, 1)


# ---------------------------
# SHARED GPS STATE (updated by background thread)
# ---------------------------
gps_lock = threading.Lock()
latest_gps = {
    "latitude": None,
    "longitude": None,
    "speed_knots": None,
    "fix_valid": False,
    "last_update": None,
    # Separately tracked so a lost fix never loses the vehicle's last
    # confirmed position — only ever updated on an actual valid fix.
    "last_known_latitude": None,
    "last_known_longitude": None,
    "last_known_time": None,
}


def gps_reader_thread():
    try:
        ser = serial.Serial(GPS_UART_PORT, GPS_BAUD_RATE, timeout=1)
    except Exception as e:
        print(f"GPS serial port could not be opened: {e}")
        return

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
                else:
                    latest_gps["fix_valid"] = False
                latest_gps["last_update"] = time.time()


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
        self.cap = cv2.VideoCapture(src)
        if width:
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        if height:
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.lock = threading.Lock()
        self.frame = None
        self.ok = self.cap.isOpened()
        self.stopped = False
        self.thread = threading.Thread(target=self._update, daemon=True)
        self.thread.start()

    def _update(self):
        while not self.stopped:
            ret, frame = self.cap.read()
            if ret:
                with self.lock:
                    self.frame = frame
                    self.ok = True
            else:
                self.ok = False

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
# maxsize=1 with drop-if-full behavior: if the network is slow, we skip
# stale telemetry rather than backing up an ever-growing queue in RAM
# (the offline SQLite buffer already handles real persistence/retry).
telemetry_queue = queue.Queue(maxsize=5)
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


def queue_put_latest(q, item):
    """Puts an item on a queue, dropping the oldest pending item first if
    the queue is full, so we always send the freshest data, not the
    stalest, when the network/radio has fallen behind."""
    try:
        q.put_nowait(item)
    except queue.Full:
        try:
            q.get_nowait()
        except queue.Empty:
            pass
        try:
            q.put_nowait(item)
        except queue.Full:
            pass


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
    "alcohol_ratio": None, "alcohol_ppm": None,
    "network_online": True, "buffer_count": 0,
    "buzzer_active": False, "lcd_message": None,
    "updated_at": None,
}

dashboard_app = Flask(__name__)


def update_dashboard_frame(frame):
    """Encodes the current annotated frame to JPEG and stores it for
    streaming. Called once per main-loop iteration."""
    global latest_frame_jpeg
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if ok:
        with dashboard_lock:
            latest_frame_jpeg = buf.tobytes()


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
<html>
<head>
    <title>TransitGuard Live Monitor</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body { font-family: -apple-system, sans-serif; background: #111; color: #eee;
               margin: 0; padding: 16px; }
        h1 { font-size: 20px; margin-bottom: 12px; }
        .layout { display: flex; flex-wrap: wrap; gap: 20px; }
        img { max-width: 100%; width: 640px; border-radius: 8px; border: 2px solid #333; }
        .panel { background: #1c1c1c; border-radius: 8px; padding: 16px; min-width: 260px; }
        .row { display: flex; justify-content: space-between; padding: 6px 0;
               border-bottom: 1px solid #2a2a2a; font-size: 14px; }
        .row span:first-child { color: #999; }
        .state { font-size: 22px; font-weight: bold; padding: 10px; border-radius: 6px;
                 text-align: center; margin-bottom: 12px; }
        .NORMAL { background: #1e4620; color: #6fdc76; }
        .DROWSY { background: #4a3b12; color: #ffcc4d; }
        .CRITICAL_FATIGUE { background: #4a1414; color: #ff5c5c; }
        .ALCOHOL_ALERT { background: #3a1a4a; color: #d98cff; }
    </style>
</head>
<body>
    <h1>TransitGuard Live Monitor</h1>
    <div class="layout">
        <img src="/video_feed" alt="Live camera feed">
        <div class="panel">
            <div id="state" class="state NORMAL">NORMAL</div>
            <div class="row"><span>EAR</span><span id="ear">--</span></div>
            <div class="row"><span>MAR</span><span id="mar">--</span></div>
            <div class="row"><span>PERCLOS</span><span id="perclos">--</span></div>
            <div class="row"><span>Head pitch</span><span id="pitch">--</span></div>
            <div class="row"><span>GPS</span><span id="gps">--</span></div>
            <div class="row"><span>MQ-3 ratio</span><span id="alcohol">--</span></div>
            <div class="row"><span>Network</span><span id="network">--</span></div>
            <div class="row"><span>Offline buffer</span><span id="buffer">--</span></div>
            <div class="row"><span>Buzzer</span><span id="buzzer">--</span></div>
            <div class="row"><span>LCD</span><span id="lcd">--</span></div>
            <div class="row"><span>Last update</span><span id="updated">--</span></div>
        </div>
    </div>
    <script>
        async function refresh() {
            const res = await fetch('/data');
            const m = await res.json();

            const stateEl = document.getElementById('state');
            stateEl.textContent = m.driver_state;
            stateEl.className = 'state ' + m.driver_state;

            document.getElementById('ear').textContent = m.ear !== null ? m.ear.toFixed(3) : '--';
            document.getElementById('mar').textContent = m.mar !== null ? m.mar.toFixed(3) : '--';
            document.getElementById('perclos').textContent = m.perclos !== null ? (m.perclos*100).toFixed(1) + '%' : '--';
            document.getElementById('pitch').textContent = m.pitch !== null ? m.pitch.toFixed(1) + '°' : '--';

            let gpsText = 'no fix yet';
            if (m.fix_valid) {
                gpsText = m.latitude.toFixed(5) + ', ' + m.longitude.toFixed(5);
            } else if (m.using_last_known && m.latitude !== null) {
                gpsText = m.latitude.toFixed(5) + ', ' + m.longitude.toFixed(5) +
                          ' (stale ' + Math.round(m.location_age_sec) + 's)';
            }
            document.getElementById('gps').textContent = gpsText;

            document.getElementById('alcohol').textContent = m.alcohol_ratio !== null ? m.alcohol_ratio.toFixed(2) : '--';
            document.getElementById('network').textContent = m.network_online ? 'Online (WiFi)' : 'Offline (LoRa fallback)';
            document.getElementById('buffer').textContent = m.buffer_count + ' pending';
            document.getElementById('buzzer').textContent = m.buzzer_active ? 'ON' : 'off';
            document.getElementById('lcd').textContent = m.lcd_message || '--';
            document.getElementById('updated').textContent = m.updated_at || '--';
        }
        setInterval(refresh, 1000);
        refresh();
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
        return jsonify(dict(latest_metrics))


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
        alert_active = driver_state != "NORMAL"

        if driver_state != last_actuator_state:
            queue_put_latest(actuator_queue, driver_state)
            last_actuator_state = driver_state

        # --- Overlays ---
        if location["fix_valid"]:
            loc_text = f"GPS: {location['latitude']:.5f}, {location['longitude']:.5f}"
        elif location.get("using_last_known"):
            age = location.get("location_age_sec") or 0
            loc_text = (f"GPS(stale {age:.0f}s): "
                        f"{location['latitude']:.5f}, {location['longitude']:.5f}")
        else:
            loc_text = "GPS: no fix yet"
        alc_text = (f"MQ-3 ratio: {alcohol['ratio']:.2f}"
                    if alcohol["ratio"] is not None else "MQ-3: no reading yet")

        color = (0, 0, 255) if alert_active else (0, 255, 0)
        cv2.putText(frame, status_text, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        cv2.putText(frame, f"State: {driver_state}", (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        cv2.putText(frame, loc_text, (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)
        cv2.putText(frame, alc_text, (10, 110), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 0), 1)

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

        # --- Supabase: immediate on alert, otherwise every PUBLISH_INTERVAL_SEC (non-blocking) ---
        sent = None
        should_send_now = alert_active or (time.time() - last_supabase_send >= PUBLISH_INTERVAL_SEC)
        if should_send_now:
            payload = build_telemetry_payload(
                avg_ear, mar, perclos, pitch, yaw, roll, location, alcohol, driver_state,
                transport_mode=("WIFI" if is_network_online() else "LORA"),
            )
            queue_put_latest(telemetry_queue, payload)
            last_supabase_send = time.time()
            sent = "queued"  # actual success/failure is logged by the uploader thread

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
                alcohol_ratio=alcohol["ratio"], alcohol_ppm=alcohol["ppm"],
                network_online=is_network_online(), buffer_count=buffer_count(),
                buzzer_active=get_current_actuator()["buzzer_active"],
                lcd_message=get_current_actuator()["lcd_message"],
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
