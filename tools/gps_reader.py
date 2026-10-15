"""
NEO-6M GPS reader
-------------------
Reads NMEA sentences from the NEO-6M GPS module over UART and parses
out latitude, longitude, altitude, speed, satellite count, and fix
status. This is the GPS subsystem feeding into the vehicle-tracking
side of the multi-hub communication project.

Install:
    pip install pyserial pynmea2

Wiring (NEO-6M -> Pi 4, BCM/hardware UART):
    VCC -> 5V (or 3.3V, check your breakout board's regulator)
    GND -> GND
    TX  -> Pi GPIO15 (RXD)
    RX  -> Pi GPIO14 (TXD)

Usage:
    python3 gps_reader.py

Note:
    A GPS module needs a clear view of the sky to get a fix. Indoors,
    it may take a long time (or never) get a fix — test outdoors or
    near a window for your first run.
"""

import serial
import pynmea2
import time

UART_PORT = "/dev/serial0"
BAUD_RATE = 9600  # NEO-6M default


def parse_gps_line(line):
    """
    Parses a single NMEA sentence. Returns a dict of GPS data if the
    sentence is a GGA (fix data) or RMC (recommended minimum) message,
    otherwise returns None.
    """
    try:
        msg = pynmea2.parse(line)
    except pynmea2.ParseError:
        return None

    if isinstance(msg, pynmea2.types.talker.GGA):
        return {
            "type": "GGA",
            "timestamp": str(msg.timestamp),
            "latitude": msg.latitude,
            "longitude": msg.longitude,
            "altitude_m": msg.altitude,
            "num_satellites": msg.num_sats,
            "fix_quality": msg.gps_qual,  # 0 = no fix, 1 = GPS fix, 2 = DGPS fix
        }

    if isinstance(msg, pynmea2.types.talker.RMC):
        return {
            "type": "RMC",
            "timestamp": str(msg.timestamp),
            "latitude": msg.latitude,
            "longitude": msg.longitude,
            "speed_knots": msg.spd_over_grnd,
            "status": msg.status,  # 'A' = active/valid, 'V' = void/no fix
        }

    return None


def main():
    ser = serial.Serial(UART_PORT, BAUD_RATE, timeout=1)
    print("Listening for GPS data (Ctrl+C to stop)...")
    print("Waiting for satellite fix — this can take 30s to a few minutes outdoors.\n")

    while True:
        try:
            raw_line = ser.readline().decode("ascii", errors="ignore").strip()
        except serial.SerialException as e:
            print(f"Serial error: {e}")
            break

        if not raw_line.startswith("$"):
            continue

        parsed = parse_gps_line(raw_line)
        if parsed:
            if parsed["type"] == "GGA":
                if parsed["fix_quality"] == 0:
                    print("No fix yet... (satellites in view:", parsed["num_satellites"], ")")
                else:
                    print(
                        f"[GGA] Lat: {parsed['latitude']:.6f}  "
                        f"Lon: {parsed['longitude']:.6f}  "
                        f"Alt: {parsed['altitude_m']}m  "
                        f"Sats: {parsed['num_satellites']}"
                    )
            elif parsed["type"] == "RMC":
                status = "VALID" if parsed["status"] == "A" else "NO FIX"
                print(
                    f"[RMC] Lat: {parsed['latitude']:.6f}  "
                    f"Lon: {parsed['longitude']:.6f}  "
                    f"Speed: {parsed['speed_knots']} knots  "
                    f"Status: {status}"
                )

        time.sleep(0.05)


if __name__ == "__main__":
    main()
