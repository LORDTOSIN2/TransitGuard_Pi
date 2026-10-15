# Raspberry Pi 4 — hardware, serials, wiring and sensors

**Host:** `10.196.74.212` (hostname `john`) · **Access:** SSH as user `john` · **Collected:** 2026-10-15, ~16:01 local (Pi timezone UTC+1 / WAT)
**Method:** read-only inspection only — nothing was changed or installed. Every command is listed in Appendix A.

> **Clock caveat.** `uptime` reports 48 minutes (rebooted ≈15:13 today), while `last` and `systemctl status` claim a start on 2026-10-13 and record `crash` entries. The Pi has no RTC, so anything timestamped before the NTP sync is unreliable. Journal lines from 15:13 today onward are consistent with `uptime`.

---

## 1. Board and serial labels

| Field | Value |
|---|---|
| Board model | Raspberry Pi 4 Model B Rev 1.4 |
| Device-tree compatible | `raspberrypi,4-model-b`, `brcm,bcm2711` |
| SoC | Broadcom BCM2711 (quad-core Cortex-A72) |
| Revision code | `d03114` |
| RAM | **8 GB** (`MemTotal: 8008488 kB`, `free`: 7820 MB total, 565 MB used) |
| **Board serial** | **`1000000098015d37`** |
| Firmware build | May 21 2026 — `288930ab4712...` (clean) (release) (start) |
| Bootloader EEPROM | 2025-05-08, `69471177ba7e...` — **update available** (`sudo rpi-eeprom-update -a`) |
| OS / kernel | Debian GNU/Linux 12 (bookworm) / `6.12.93+rpt-rpi-v8` |
| Hostname | `john` |
| Uptime | 48 min, load average 2.06 |
| HAT | **none** — no HAT ID EEPROM present in `/proc/device-tree/hat` |
| ARM clock (idle) | 600 MHz (`frequency(48)=600169920`); boost target 1.5 GHz |
| Core / UART clock | 500 MHz / 48 MHz |

## 2. Storage

| Field | Value |
|---|---|
| Device | `/dev/mmcblk0` — SD card |
| CID name / serial / date | `SA08G` / **`0x46a8eee1`** / `11/2017` |
| CID manfid / oemid | `0x000002` / `0x544d` |
| Capacity | 7.2 GiB (8 GB class, SanDisk-style `SA08G` designation) |
| `mmcblk0p1` | 512 MB → `/boot/firmware`, 78 MB used (16 %) |
| `mmcblk0p2` | 6.7 GB → `/`, **5.8 GB used, 407 MB free — 94 % full** |
| Swap | 405 MB (unused) |
| Other block devices | none — no USB or NVMe storage attached |

The card is 2017-vintage media with its root filesystem 94 % full, while `~/fatigue/vehicle_monitor_log.csv` grows continuously and `~/.transitguard_buffer.db` (1.1 MB) re-grows whenever uploads fail. Worth watching for wear and for write failures.

## 3. USB peripherals

| Port path | IDs | Strings | Serial |
|---|---|---|---|
| `1-1` | `2109:3431` | "USB2.0 Hub" (on-board VIA Labs hub) | none |
| `1-1.2` | `1903:8328` | "Generic" / "HD camera" — UVC (`uvcvideo`) | **none** |
| `usb1` | `1d6b:0002` | Linux xHCI USB 2.0 root hub | `0000:01:00.0` |
| `usb2` | `1d6b:0003` | Linux xHCI USB 3.0 root hub | `0000:01:00.0` |

Only one external device: a UVC webcam on hub port 2, exposed as `/dev/video0`–`/dev/video1` (node name "HD camera : HD camera"). No USB device here exposes a serial string, so identify them by `vendor:product` plus port path. Camera is USB only — no CSI camera is attached (`rpicam-hello --list-cameras` → "No cameras available!"; `vcgencmd get_camera` → supported=0 detected=0).

## 4. Network interfaces

| Interface | State | MAC | Address |
|---|---|---|---|
| `wlan0` | UP (in use) | `e4:5f:01:0b:b7:ce` | `10.196.74.212/24` (regdom NG) |
| `eth0` | DOWN, no carrier | `e4:5f:01:0b:b7:cd` | — |
| `lo` | UP | — | 127.0.0.1/8, ::1/128 |

Listening TCP (all interfaces unless noted): `22` SSH, `5900` wayvnc, `8080` TransitGuard web UI, `631` CUPS (localhost only). The machine is WiFi-only right now; Ethernet is unplugged.

## 5. GPIO / pin assignments

Live state from `pinctrl get`, cross-checked against the wiring declared in `~/fatigue/vehicle_monitor8.py` (docstring + constants). **Every assigned line matches the code** — the live pin state is the app's state.

| GPIO | Header pin | Live state (`pinctrl`) | Assigned to | Match |
|---|---|---|---|---|
| 0 | 27 | alt4 `TXD2`, hi | ATK-LORA-01 UART2 TXD (`/dev/ttyAMA2`) | ✓ `dtoverlay=uart2` |
| 1 | 28 | alt4 `RXD2`, hi | ATK-LORA-01 RXD | ✓ |
| 2 | 3 | alt0 `SDA1`, pull-up | ADS1115 SDA (I²C1) | ✓ |
| 3 | 5 | alt0 `SCL1`, pull-up | ADS1115 SCL | ✓ |
| 4, 5, 6 | 7, 29, 31 | input, pull-up | unused | — |
| 7 | 26 | output, hi | 16×2 LCD D4 | ✓ |
| 8 | 24 | output, lo | 16×2 LCD E | ✓ (see note) |
| 9, 10, 11 | 21, 19, 23 | alt0 `SPI0_MISO/MOSI/SCLK` | SPI0 enabled in `config.txt`, **nothing attached** | ⚠ |
| 12 | 32 | output, hi | 16×2 LCD D5 | ✓ |
| 13 | 33 | input, pull-down | unused | — |
| 14 | 8 | UART function (TXD1) | NEO-6M GPS RX line | ✓ |
| 15 | 10 | UART function (RXD1) | NEO-6M GPS TX line | ✓ |
| 16 | 36 | output, lo | 16×2 LCD D6 | ✓ |
| 17, 19, 21, 22 | 11, 35, 40, 15 | input | unused | — |
| 18 | 12 | output, hi | Buzzer | ✓ |
| 20 | 38 | output, lo | 16×2 LCD D7 | ✓ |
| 23 | 16 | output, lo | ATK-LORA-01 M0 (held LOW = normal mode) | ✓ |
| 24 | 18 | output, lo | ATK-LORA-01 M1 (held LOW) | ✓ |
| 25 | 22 | output, hi | 16×2 LCD RS | ✓ |
| 26, 27 | 37, 13 | input, pull-down | unused | — |

**Note — SPI0 conflict:** GPIO7 and GPIO8 are SPI0's two chip-selects (`CE1`/`CE0`), and both are driven as LCD data/enable lines. SPI0 is enabled (`dtparam=spi=on`, `/dev/spidev0.0`, `/dev/spidev0.1` exist) but has no clients, and it cannot be used alongside the LCD as currently wired. Either drop `dtparam=spi=on` or move the LCD off 7/8.

**Not available as user pins:** GPIO28/29 (RGMII MDIO/MDC), 30/31 (CTS0/RTS0), 32/33 (TXD0/RXD0 — the PL011, consumed by Bluetooth), 34–39 (SD1), 40/41 (PWM1), 43 (SPI-flash CE), 44/45 (ID EEPROM), 46–57 (RGMII), 100/101 (BT_ON / WL_ON). GPIO42 drives the activity LED.

## 6. Buses and what is on them

| Bus | Enabled by | Devices found |
|---|---|---|
| I²C1 — GPIO2/3 | `dtparam=i2c_arm=on` | **Live scan: address `0x48` only** → ADS1115 ADC |
| I²C-20 / I²C-21 | SoC auxiliary controllers (`fef04500`, `fef09500`) | no clients |
| SPI0 | `dtparam=spi=on` | none — `spidev` loaded, 0 users |
| Serial — `/dev/serial0` → **`ttyS0`** | `enable_uart=1` | NEO-6M GPS @ 9600 baud on GPIO14/15 |
| Serial — `/dev/ttyAMA2` (PL011) | `dtoverlay=uart2` | ATK-LORA-01 @ 9600 baud on GPIO0/1 |
| PL011 `ttyAMA1` | on-board | Bluetooth (`hci_uart_bcm`) |
| 1-Wire | *not enabled* | **no devices** — no DS18B20 or other w1 probes |
| hwmon | on-board | `cpu_thermal`, `rpi_volt` — no external sensor drivers |

`/dev/serial0` resolves to the **mini UART (ttyS0, fe215040.serial)**, not the PL011 — the PL011 is taken by Bluetooth. The mini UART has no FIFO and its baud derives from the VPU clock (500 MHz here), which is fine on this fixed-clock Pi 4 but is the classic source of dropped NMEA bytes under heavy CPU load. This box is running at load ≈2.06, so it is worth logging GPS sentence-integrity rather than assuming it is clean.

## 7. Sensors, wiring and live status

| Sensor / peripheral | Interface & pins | Live evidence |
|---|---|---|
| MQ-3 alcohol sensor | via ADS1115 ADC at I²C `0x48`, A0 | **`0x48` answered a live I²C scan** — chain is wired and alive |
| NEO-6M GPS | `ttyS0` @9600, GPIO14/15 | no error in the last 400 journal lines; fix state is shown on the :8080 UI |
| ATK-LORA-01 | `ttyAMA2` @9600, GPIO0/1, M0/M1 = GPIO23/24 LOW | journal: `LoRa module ready on /dev/ttyAMA2`; M0/M1 confirmed LOW |
| 16×2 LCD (4-bit) | RS=GPIO25, E=GPIO8, D4–D7=GPIO7/12/16/20 | journal: `LCD boot screen showing IP address: 10.196.74.212`; pin states match |
| Buzzer | GPIO18 | pin driven high (idle per `BUZZER_ACTIVE_HIGH` logic) |
| Driver-fatigue camera | USB UVC webcam `1903:8328` → `/dev/video0` | MediaPipe face-landmark pipeline in `vehicle_monitor8.py` |
| SoC thermal | `hwmon0` `cpu_thermal` | 80.3 °C (`vcgencmd measure_temp`), 80.8 °C at second read |

Nothing else is attached: no 1-Wire probes, no I²C devices besides the ADS1115, no HAT, no CSI camera, no USB serial adapters (`/dev/ttyUSB*`, `/dev/ttyACM*` absent).

## 8. Health flags found along the way

1. **Under-voltage and throttling, active now** — `vcgencmd get_throttled` = `0x50005`: bit 0 (under-voltage detected) + bit 2 (currently throttled) set, with bits 16/18 set for past occurrences. With CPU at 80.3 °C and the `crash`/reboot records in `last`, the power supply or cable is the prime suspect for the earlier unclean shutdowns.
2. **Thermal headroom nearly gone** — 80.3–80.8 °C at load ≈2.06 puts the Pi at the 80 °C soft-throttle threshold.
3. **`transitguard.service` pegs ~99 % of one core** (`CPU: 1h4min` in ~48 min of uptime), running as `john` — a sudo-capable account.
4. **Root filesystem 94 % full** (407 MB free) on 2017-vintage SD media, with a growing CSV log and SQLite buffer.
5. **Telemetry uploads are failing** — `ConnectTimeoutError` to `hfzqfcuezuxcgaifxqok.supabase.co:443`, so the app buffers locally and falls back to LoRa (`[OFFLINE] ... falling back to LoRa`). Either the network blocks it or routing/DNS is failing.
6. **`/home/john/fatigue/.env` is world-readable (0644)** while `/etc/transitguard.env` is correctly `0640 root:john`.
7. **Bootloader EEPROM update pending** (2025-05-08 → newer available).

---

## Appendix A — commands used

```sh
# identity
cat /proc/device-tree/model ; cat /proc/device-tree/compatible
grep -E 'Revision|Serial|Model|Hardware' /proc/cpuinfo
cat /proc/device-tree/serial-number
vcgencmd version ; vcgencmd bootloader_version ; rpi-eeprom-update
grep MemTotal /proc/meminfo ; free -m ; uptime

# storage
lsblk -o NAME,SIZE,TYPE,MODEL,SERIAL,MOUNTPOINT -e7
cat /sys/block/mmcblk0/device/{name,serial,date,manfid,oemid}
df -h / /boot/firmware

# usb
lsusb ; lsusb -t
for f in /sys/bus/usb/devices/*/; do cat $f/idVendor $f/idProduct $f/manufacturer $f/product $f/serial; done
cat /sys/class/video4linux/video0/name

# wiring / buses
grep -v '^#' /boot/firmware/config.txt | grep -v '^$' ; dtoverlay -l
pinctrl get ; pinctrl funcs
ls -l /dev/i2c* /dev/spidev* /dev/ttyUSB* /dev/ttyACM* /dev/ttyAMA* /dev/gpiomem
ls -l /sys/bus/i2c/devices/ /sys/bus/spi/devices/ /sys/bus/w1/devices/
ls -l /dev/serial0 /dev/serial1 /dev/ttyAMA0 /dev/ttyS0
cat /boot/firmware/cmdline.txt

# sensors
/some/venv/bin/python3 -c 'import board,busio; i2c=busio.I2C(board.SCL,board.SDA); print(i2c.scan())'
for h in /sys/class/hwmon/hwmon*/; do cat $h/name; cat $h/temp*_input; done
vcgencmd measure_temp ; vcgencmd measure_volts ; vcgencmd get_throttled
vcgencmd measure_clock core ; vcgencmd measure_clock uart ; vcgencmd measure_clock arm

# service + logs
systemctl cat transitguard.service ; systemctl status transitguard.service
journalctl -u transitguard -n 400 --no-pager
dmesg | grep -iE 'uart|serial|ttyAMA|ttyS0|i2c|uvc'
ps -ef | grep -i python
last -n 6 ; ip -br link ; ip -br addr
```

## Appendix B — raw `pinctrl get` excerpt (user pins)

```
 0: a4    pn | hi // ID_SDA/GPIO0 = TXD2
 1: a4    pu | hi // ID_SCL/GPIO1 = RXD2
 2: a0    pu | hi // GPIO2 = SDA1
 3: a0    pu | hi // GPIO3 = SCL1
 4: ip    pu | hi // GPIO4 = input
 7: op -- pn | hi // GPIO7 = output
 8: op -- pn | lo // GPIO8 = output
 9: a0    pd | lo // GPIO9 = SPI0_MISO
10: a0    pd | lo // GPIO10 = SPI0_MOSI
11: a0    pd | lo // GPIO11 = SPI0_SCLK
12: op -- pn | hi // GPIO12 = output
14: a5    pn | hi // GPIO14 = TXD1
15: a5    pu | hi // GPIO15 = RXD1
16: op -- pn | lo // GPIO16 = output
18: op -- pn | hi // GPIO18 = output
20: op -- pn | lo // GPIO20 = output
23: op -- pn | lo // GPIO23 = output
24: op -- pn | lo // GPIO24 = output
25: op -- pn | hi // GPIO25 = output
```

## Appendix C — open items needing `sudo`

- `i2cdetect -y 1` — confirm the full I²C address map (needs `i2c-tools`; the venv scan already proved `0x48`).
- `sudo rpi-eeprom-update -a` — apply the pending bootloader update.
- `/etc/wayvnc/config` — VNC credentials for the wayvnc server on port 5900 (root-only, 0600; RSA key + TLS certs sit beside it).
- Power supply measurement / PSU swap to clear the `0x50005` throttle state.
