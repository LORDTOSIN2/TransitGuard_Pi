"""
ATK-LORA-01 configuration and test script
--------------------------------------------
Confirms the module is alive and lets you set its radio parameters
(channel, address, baud rate, power) via AT commands, before you
have a second module to test actual over-the-air transmission.

Install:
    pip install pyserial RPi.GPIO

Wiring (BCM numbering):
    VCC  -> 3.3V
    GND  -> GND
    TXD  -> Pi GPIO15 (RXD)
    RXD  -> Pi GPIO14 (TXD)
    M0   -> Pi GPIO23
    M1   -> Pi GPIO24

Usage:
    python3 lora_at_config.py
"""

import serial
import time
import RPi.GPIO as GPIO

M0_PIN = 23
M1_PIN = 24
UART_PORT = "/dev/serial0"   # Pi's hardware UART
CONFIG_BAUD = 9600           # ATK-LORA-01 default config baud rate

GPIO.setmode(GPIO.BCM)
GPIO.setup(M0_PIN, GPIO.OUT)
GPIO.setup(M1_PIN, GPIO.OUT)


def enter_config_mode():
    """M0=1, M1=1 puts the module into AT command mode."""
    GPIO.output(M0_PIN, GPIO.HIGH)
    GPIO.output(M1_PIN, GPIO.HIGH)
    time.sleep(0.1)  # allow mode switch to settle


def enter_normal_mode():
    """M0=0, M1=0 puts the module into transparent transmission mode."""
    GPIO.output(M0_PIN, GPIO.LOW)
    GPIO.output(M1_PIN, GPIO.LOW)
    time.sleep(0.1)


def send_at_command(ser, command, wait=0.2):
    ser.write((command + "\r\n").encode())
    time.sleep(wait)
    response = ser.read(ser.in_waiting or 64)
    return response.decode(errors="ignore").strip()


def main():
    enter_config_mode()
    ser = serial.Serial(UART_PORT, CONFIG_BAUD, timeout=1)

    print("Checking module is alive...")
    print("AT ->", send_at_command(ser, "AT"))

    print("Reading current parameters...")
    print("AT+PARAMETER? ->", send_at_command(ser, "AT+PARAMETER?"))

    print("Reading version...")
    print("AT+VERSION? ->", send_at_command(ser, "AT+VERSION?"))

    # Example: set a channel/address (adjust to your module's exact
    # AT command syntax from its manual before uncommenting)
    # print(send_at_command(ser, "AT+ADDRESS=1"))
    # print(send_at_command(ser, "AT+PARAMETER=10,7,1,4"))

    ser.close()
    enter_normal_mode()
    print("Module returned to normal transmission mode.")


if __name__ == "__main__":
    main()
