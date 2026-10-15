"""
MQ-3 Alcohol Sensor Reader (via ADS1115 ADC)
-----------------------------------------------
Reads the MQ-3 gas sensor's analog output through an ADS1115 ADC over
I2C, and converts it to an approximate alcohol concentration using the
sensor's Rs/Ro ratio.

IMPORTANT — calibration required before readings are meaningful:
  1. Power the sensor and let it warm up for at least 20-30 minutes
     (first-ever use: ideally 24-48 hours) before trusting readings.
  2. Run this script in CALIBRATE mode in clean air (no alcohol nearby)
     to determine R0, your sensor's baseline resistance.
  3. Put that R0 value into the config below, then switch to READ mode.

Install:
    pip install adafruit-blinka adafruit-circuitpython-ads1x15

Wiring:
    MQ-3 VCC  -> 5V
    MQ-3 GND  -> GND
    MQ-3 AOUT -> ADS1115 A0
    ADS1115 VDD -> Pi 3.3V
    ADS1115 GND -> Pi GND
    ADS1115 SCL -> Pi GPIO3 (SCL)
    ADS1115 SDA -> Pi GPIO2 (SDA)

Usage:
    python3 mq3_reader.py calibrate    # run in clean air, note the R0 printed
    python3 mq3_reader.py read         # after setting R0 below
"""

import sys
import time
import board
import busio
import adafruit_ads1x15.ads1115 as ADS
from adafruit_ads1x15.analog_in import AnalogIn
from adafruit_ads1x15 import ads1x15

# ---------------------------
# CONFIGURATION
# ---------------------------
RL_VALUE = 10.0        # load resistance on the MQ-3 board, in kOhms (check your board, often 10k or 4.7k)
R0 = 10.0               # PLACEHOLDER — replace with the value from your calibration run
VCC = 5.0               # supply voltage to the MQ-3 sensor circuit

# Approximate curve constants for MQ-3 alcohol response (from datasheet
# log-log curve fit). These are commonly used starting values — treat
# them as a starting point, not ground truth, for your thesis writeup.
CURVE_A = 0.4
CURVE_B = -1.5

ALERT_RATIO_THRESHOLD = 0.4  # Rs/Ro ratio below this suggests elevated alcohol; tune during testing


def setup_adc():
    i2c = busio.I2C(board.SCL, board.SDA)
    ads = ADS.ADS1115(i2c)
    channel = AnalogIn(ads, ads1x15.Pin.A0)
    return channel


def voltage_to_rs(voltage):
    """Converts the ADC voltage reading into the sensor's Rs (sensing resistance)."""
    if voltage <= 0:
        return float("inf")
    return (VCC - voltage) * RL_VALUE / voltage


def rs_to_ppm(rs, r0):
    """
    Rough PPM estimate from the Rs/Ro ratio using the datasheet curve.
    This is an approximation — for a thesis-quality result, derive your
    own curve fit constants from the MQ-3 datasheet graph or your own
    calibration against known alcohol concentrations.
    """
    ratio = rs / r0
    if ratio <= 0:
        return 0.0
    ppm = CURVE_A * (ratio ** CURVE_B)
    return ppm, ratio


def calibrate_mode(channel, duration_sec=60):
    print(f"Calibrating in clean air for {duration_sec} seconds...")
    print("Make sure there is NO alcohol source nearby.\n")

    readings = []
    start = time.time()
    while time.time() - start < duration_sec:
        voltage = channel.voltage
        rs = voltage_to_rs(voltage)
        readings.append(rs)
        print(f"Voltage: {voltage:.3f}V  Rs: {rs:.2f} kOhm")
        time.sleep(1)

    avg_rs = sum(readings) / len(readings)
    print(f"\nCalibration complete.")
    print(f"Average Rs in clean air: {avg_rs:.2f} kOhm")
    print(f"Set R0 = {avg_rs:.2f} in the CONFIGURATION section of this script, then run in 'read' mode.")


def read_mode(channel):
    print("Reading MQ-3 sensor (Ctrl+C to stop)...")
    print(f"Using R0 = {R0} kOhm — make sure this came from your own calibration run.\n")

    while True:
        voltage = channel.voltage
        rs = voltage_to_rs(voltage)
        ppm, ratio = rs_to_ppm(rs, R0)

        alert = ratio < ALERT_RATIO_THRESHOLD
        status = "ALCOHOL DETECTED" if alert else "normal"

        print(f"Voltage: {voltage:.3f}V  Rs: {rs:.2f}kOhm  Rs/Ro: {ratio:.2f}  "
              f"Est. PPM: {ppm:.1f}  [{status}]")
        time.sleep(1)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("calibrate", "read"):
        print("Usage: python3 mq3_reader.py [calibrate|read]")
        return

    channel = setup_adc()

    if sys.argv[1] == "calibrate":
        calibrate_mode(channel)
    else:
        read_mode(channel)


if __name__ == "__main__":
    main()
