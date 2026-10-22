#!/usr/bin/env python3
"""
TransitGuard GPS open-sky availability trials.

Primary workflow (one trial per power-on, result shown on the LCD):

    # once, over SSH, while the Pi is on:
    python3 tools/run_gps_trials.py --install --arm \
        --target 3 --max-sec 180 --post-fix-sec 30
    # then power-cycle. Each boot: app loads, test runs, LCD says DONE.

Each boot runs exactly one trial from the armed run, writes
logs/gps_trials/run_<UTC>/trial_<n>/, updates the boot state and leaves the
result on the 16x2 LCD ("POWER OFF"). The app is intentionally not restarted
for that boot, so the LCD result stays visible. After the third trial the state
is disarmed, so later boots are completely normal.

Trials stop early once a fix has been held for --post-fix-sec (default 30 s),
with a hard cap of --max-sec (default 180 s = 3 min). Each trial follows a full
Pi power cycle, so the receiver is genuinely cold; a UBX-CFG-RST
(navBbrMask=0xFFFF) is also issued as a secondary control. The Pi boot ID and
uptime are recorded as the primary cold-start evidence, and the NMEA gap after
the reset shows whether the software reset was observed.

Metrics only: no coordinates are written.

Other modes
-----------
    --probe                 one 90 s probe (module ID + reset check), no state
    --trials 3 --trial-min 10
                            three trials in one boot (no LCD state machinery)
    --start-now             arm and start the first trial without a reboot
    --disarm                cancel a remaining campaign

Analysis: tools/analyze_gps_trials.py
"""
import argparse
import csv
import datetime as dt
import json
import os
import signal
import statistics
import subprocess
import sys
import time
import traceback

APP = "/home/john/fatigue"
LOGS = os.path.join(APP, "logs")
RUN_ROOT = os.path.join(LOGS, "gps_trials")
PROBE_ROOT = os.path.join(LOGS, "gps_probe")
STATE_PATH = os.path.join(RUN_ROOT, "BOOT_STATE.json")
DONE_PATH = os.path.join(RUN_ROOT, "BOOT_STATE.done.json")
UNIT_PATH = "/etc/systemd/system/tg-gps-trial.service"
PORT = os.environ.get("GPS_UART_PORT", "/dev/serial0")
BAUD = int(os.environ.get("GPS_BAUD_RATE", "9600"))
MAX_TEMP_C = 86.0
MIN_DISK_MB = 100
SENTENCES = (("GGA", (0xF0, 0x00)), ("GSA", (0xF0, 0x02)),
             ("GSV", (0xF0, 0x03)), ("RMC", (0xF0, 0x04)),
             ("VTG", (0xF0, 0x05)))
LCD_COLS, LCD_ROWS = 16, 2
LCD_PINS = dict(pin_rs=25, pin_e=8, pins_data=[7, 12, 16, 20])

_stop = {"flag": False, "reason": None}


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def stamp():
    return now_utc().strftime("%Y%m%dT%H%M%SZ")


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def sh(cmd, timeout=30):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def sudo(cmd, timeout=60):
    return sh(["sudo", "-n"] + cmd, timeout=timeout)


def temp_c():
    try:
        return round(int(read_text("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000.0, 1)
    except (ValueError, OSError):
        return None


def throttled():
    rc, out, _ = sh(["vcgencmd", "get_throttled"])
    return out.split("=", 1)[1] if "=" in out else None


def disk_free_mb():
    st = os.statvfs("/")
    return round(st.f_bavail * st.f_frsize / (1024 * 1024), 1)


def sha256(path):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def med(xs):
    return round(statistics.median(xs), 2) if xs else None


def sync_disk():
    try:
        os.sync()
    except AttributeError:
        pass


def boot_id():
    return read_text("/proc/sys/kernel/random/boot_id").strip() or None


def uptime_s():
    try:
        return round(float(read_text("/proc/uptime").split()[0]), 1)
    except (ValueError, IndexError, OSError):
        return None


# --------------------------------------------------------------------------
# UBX helpers
# --------------------------------------------------------------------------
def ubx_checksum(data):
    a = b = 0
    for byte in data:
        a = (a + byte) & 0xFF
        b = (b + a) & 0xFF
    return bytes((a, b))


def ubx_frame(cls, mid, payload=b""):
    body = bytes((cls, mid)) + len(payload).to_bytes(2, "little") + payload
    return b"\xB5\x62" + body + ubx_checksum(body)


def ubx_cfg_msg(cls, mid, rate=1):
    return ubx_frame(0x06, 0x01, bytes((cls, mid, rate)))


def ubx_cfg_cold_start():
    # navBbrMask=0xFFFF clears almanac, ephemeris, health, klobuchar, position,
    # clock drift, oscillator and UTC; resetMode=0x02 is a controlled GPS reset.
    return ubx_frame(0x06, 0x04, b"\xFF\xFF\x02\x00")


def read_ubx(ser, cls, mid, timeout=2.0):
    import time as _t
    buf = b""
    deadline = _t.monotonic() + timeout
    while _t.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf += chunk
            marker = bytes((0xB5, 0x62, cls, mid))
            idx = buf.find(marker)
            if idx >= 0 and len(buf) >= idx + 6:
                length = int.from_bytes(buf[idx + 4:idx + 6], "little")
                if len(buf) >= idx + 8 + length:
                    return buf[idx + 6:idx + 6 + length]
        else:
            _t.sleep(0.05)
    return None


def module_info(ser):
    ser.reset_input_buffer()
    ser.write(ubx_frame(0x0A, 0x04))
    ser.flush()
    payload = read_ubx(ser, 0x0A, 0x04, timeout=2.0)
    if not payload:
        return {"ubx": False, "sw": None, "hw": None, "extensions": []}
    sw = payload[:30].split(b"\x00")[0].decode("ascii", "replace")
    hw = payload[30:40].split(b"\x00")[0].decode("ascii", "replace")
    ext = payload[40:]
    strings = []
    i = 0
    while i < len(ext):
        n = ext[i]
        if n == 0 or i + 1 + n > len(ext):
            break
        strings.append(ext[i + 1:i + 1 + n].decode("ascii", "replace"))
        i += 1 + n
    return {"ubx": True, "sw": sw, "hw": hw, "extensions": strings[:6]}


def configure_sentences(ser):
    for _, (cls, mid) in SENTENCES:
        ser.write(ubx_cfg_msg(cls, mid, 1))
    ser.flush()


# --------------------------------------------------------------------------
# NMEA state
# --------------------------------------------------------------------------
def parse_line(line, st, events, first_after_reset):
    import pynmea2
    try:
        text = line.decode("ascii", "ignore").strip()
        if not text.startswith("$"):
            return False
        msg = pynmea2.parse(text)
    except Exception:  # noqa: BLE001
        return False

    now = time.monotonic()
    st["nmea"] += 1
    if first_after_reset["t"] is None:
        first_after_reset["t"] = now

    kind = type(msg).__name__
    if kind == "GGA":
        st["gga"] += 1
        try:
            quality = int(msg.gps_qual or 0)
        except (TypeError, ValueError):
            quality = 0
        st["quality"] = quality
        try:
            st["sats_used"] = int(msg.num_sats)
        except (TypeError, ValueError):
            pass
        try:
            st["hdop"] = float(msg.horizontal_dil)
        except (TypeError, ValueError):
            pass
        valid = quality > 0
        if valid:
            if st["first_fix_mono"] is None:
                st["first_fix_mono"] = now
                events.append({"event": "fix_acquired",
                               "elapsed_s": round(now - st["reset_mono"], 2)})
            elif not st["valid"]:
                events.append({"event": "fix_regained",
                               "elapsed_s": round(now - st["reset_mono"], 2)})
            st["last_fix_mono"] = now
            if st["fix_hold_start"] is None:
                st["fix_hold_start"] = now
        else:
            if st["valid"] and st["first_fix_mono"] is not None:
                st["fix_losses"] += 1
                events.append({"event": "fix_lost",
                               "elapsed_s": round(now - st["reset_mono"], 2)})
            st["fix_hold_start"] = None
        st["valid"] = valid

    elif kind == "RMC":
        st["rmc"] += 1
        st["rmc_status"] = getattr(msg, "status", None)

    elif kind == "GSV":
        st["gsv"] += 1
        d = list(msg.data)
        try:
            total, num, in_view = int(d[0]), int(d[1]), int(d[2])
        except (ValueError, IndexError):
            total = num = 0
            in_view = None
        if num == 1:
            st["gsv_best"] = []
        for i in range(3, len(d) - 3, 4):
            snap = d[i + 3]
            if snap.isdigit():
                st["gsv_best"].append(int(snap))
        if num == total:
            if in_view is not None:
                st["sats_in_view"] = in_view
            if st["gsv_best"]:
                st["best_snr"] = max(st["gsv_best"])
    return True


def new_state(reset_mono):
    return {
        "reset_mono": reset_mono,
        "nmea": 0, "gga": 0, "gsv": 0, "rmc": 0,
        "quality": 0, "valid": False,
        "sats_used": None, "sats_in_view": None,
        "best_snr": None, "hdop": None, "rmc_status": None,
        "first_fix_mono": None, "last_fix_mono": None,
        "fix_hold_start": None, "fix_losses": 0, "gsv_best": [],
    }


# --------------------------------------------------------------------------
# LCD / buzzer
# --------------------------------------------------------------------------
def lcd_open():
    try:
        import RPi.GPIO as GPIO
        from RPLCD.gpio import CharLCD
        GPIO.setmode(GPIO.BCM)
        lcd = CharLCD(numbering_mode=GPIO.BCM, cols=LCD_COLS, rows=LCD_ROWS,
                      **LCD_PINS)
        lcd.clear()
        return lcd
    except Exception as exc:  # noqa: BLE001
        print(f"LCD unavailable: {exc}")
        return None


def lcd_show(lcd, line1, line2=""):
    if lcd is None:
        return
    try:
        lcd.cursor_pos = (0, 0)
        lcd.write_string(str(line1).ljust(LCD_COLS)[:LCD_COLS])
        if LCD_ROWS > 1:
            lcd.cursor_pos = (1, 0)
            lcd.write_string(str(line2).ljust(LCD_COLS)[:LCD_COLS])
    except Exception as exc:  # noqa: BLE001
        print(f"LCD write failed: {exc}")


def beep(times=2):
    try:
        import RPi.GPIO as GPIO
        GPIO.setup(18, GPIO.OUT)
        for _ in range(times):
            GPIO.output(18, GPIO.HIGH)
            time.sleep(0.12)
            GPIO.output(18, GPIO.LOW)
            time.sleep(0.12)
    except Exception as exc:  # noqa: BLE001
        print(f"buzzer unavailable: {exc}")


# --------------------------------------------------------------------------
# boot state
# --------------------------------------------------------------------------
def read_state():
    return read_json(STATE_PATH)


def write_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, STATE_PATH)


def clear_state(completed=True):
    state = read_state()
    if completed and state:
        state["armed"] = False
        state["completed_utc"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
        write_json(DONE_PATH, state)
    try:
        os.remove(STATE_PATH)
    except OSError:
        pass


# --------------------------------------------------------------------------
# unit install / arm
# --------------------------------------------------------------------------
UNIT_TEMPLATE = """[Unit]
Description=TransitGuard GPS open-sky trial (one per boot)
After=transitguard.service
Wants=transitguard.service
ConditionPathExists={state}

[Service]
Type=oneshot
User=john
Group=john
SupplementaryGroups=dialout gpio
WorkingDirectory={app}
ExecStartPre=/bin/sleep {delay}
ExecStart={python} {app}/tools/run_gps_trials.py --boot --boot-delay 0
TimeoutStartSec=600

[Install]
WantedBy=multi-user.target
"""


def install_unit(delay=20):
    unit = UNIT_TEMPLATE.format(state=STATE_PATH, app=APP,
                                 python=os.path.join(APP, "fatigue-env/bin/python3"),
                                 delay=delay)
    p = subprocess.run(["sudo", "-n", "tee", UNIT_PATH], input=unit, text=True,
                       capture_output=True)
    if p.returncode != 0:
        sys.exit(f"could not write {UNIT_PATH}: {p.stderr.strip()}")
    sudo(["systemctl", "daemon-reload"])
    rc, out, err = sudo(["systemctl", "enable", "tg-gps-trial.service"])
    if rc != 0:
        sys.exit(f"could not enable unit: {err}")
    print(f"installed and enabled {UNIT_PATH}")
    print("The unit runs one trial per boot while BOOT_STATE.json exists.")


def arm(args):
    if os.path.exists(STATE_PATH):
        sys.exit(f"{STATE_PATH} already exists; finish or run --disarm first")
    run_id = stamp()
    run_dir = os.path.join(RUN_ROOT, f"run_{run_id}")
    os.makedirs(run_dir, exist_ok=False)
    manifest = {
        "run": run_id,
        "mode": "boot-one-trial-per-power-on",
        "armed_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target_trials": args.target,
        "max_sec": args.max_sec,
        "post_fix_sec": args.post_fix_sec,
        "boot_delay_sec": args.boot_delay,
        "port": PORT, "baud": BAUD,
        "app_head": sh(["git", "-C", APP, "rev-parse", "HEAD"])[1],
        "vehicle_monitor_sha256": sha256(os.path.join(APP, "vehicle_monitor.py")),
        "clock_sync": sh(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])[1],
        "utc_offset": dt.datetime.now().astimezone().strftime("%z"),
        "temp_c_at_arm": temp_c(),
        "throttled_at_arm": throttled(),
        "disk_free_mb": disk_free_mb(),
        "cold_start_method": ("full Pi power cycle per trial + UBX-CFG-RST "
                              "navBbrMask=0xFFFF (receiver-level control)"),
        "coords_retained": False,
    }
    rc, ssid, _ = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    manifest["active_ssid"] = next((l.split(":", 1)[1] for l in ssid.splitlines()
                                    if l.startswith("yes:")), None)
    write_json(os.path.join(run_dir, "manifest.json"), manifest)
    state = {
        "armed": True,
        "target": args.target,
        "next_trial": 1,
        "run_dir": run_dir,
        "max_sec": args.max_sec,
        "post_fix_sec": args.post_fix_sec,
        "boot_delay": args.boot_delay,
        "trials": [],
        "armed_utc": manifest["armed_utc"],
    }
    write_state(state)
    print(f"armed: {run_dir}")
    print(f"power-cycle now. Each boot runs trial n/{args.target} "
          f"(max {args.max_sec}s, stops {args.post_fix_sec}s after a fix).")
    print("The LCD shows DONE when it is safe to power off.")


# --------------------------------------------------------------------------
# one trial
# --------------------------------------------------------------------------
def run_trial(ser, trial_no, max_sec, post_fix_sec, info, out_dir, initial_temp,
              progress=None):
    os.makedirs(out_dir, exist_ok=True)
    samples_path = os.path.join(out_dir, "samples.csv")
    events_path = os.path.join(out_dir, "events.jsonl")
    samples = []
    events = []
    temps = [t for t in (initial_temp, temp_c()) if t is not None]
    uptime_start = uptime_s()
    aborted = None
    reason = "timeout_no_fix"

    ser.reset_input_buffer()
    reset_mono = time.monotonic()
    ser.write(ubx_cfg_cold_start())
    ser.flush()

    st = new_state(reset_mono)
    first_after_reset = {"t": None}
    cfg_at = [reset_mono + s for s in (2, 4, 6, 9, 12, 16)]
    last_sample = reset_mono
    next_temp = reset_mono

    while True:
        now = time.monotonic()
        if now - reset_mono >= max_sec:
            break
        if _stop["flag"]:
            aborted = _stop["reason"] or "stopped"
            break

        if cfg_at and now >= cfg_at[0]:
            configure_sentences(ser)
            cfg_at.pop(0)

        try:
            line = ser.readline()
        except Exception as exc:  # noqa: BLE001
            aborted = f"serial read failed: {exc}"
            break

        if line:
            parse_line(line, st, events, first_after_reset)

        # Stop once the fix has been held long enough to characterise it.
        if (st["fix_hold_start"] is not None
                and now - st["fix_hold_start"] >= post_fix_sec):
            reason = "fix_hold_complete"
            break

        if now - next_temp >= 30:
            t = temp_c()
            if t is not None:
                temps.append(t)
                if t > MAX_TEMP_C:
                    aborted = f"SoC {t} C above {MAX_TEMP_C} C"
                    _stop["flag"] = True
                    break
            next_temp = now

        if now - last_sample >= 1.0:
            since_fix = (round(now - st["last_fix_mono"], 1)
                         if st["last_fix_mono"] is not None else None)
            samples.append({
                "elapsed_s": round(now - reset_mono, 2),
                "system_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "fix_valid": st["valid"],
                "fix_quality": st["quality"],
                "sats_used": st["sats_used"],
                "sats_in_view": st["sats_in_view"],
                "best_snr": st["best_snr"],
                "hdop": round(st["hdop"], 2) if st["hdop"] is not None else None,
                "sec_since_last_fix": since_fix,
            })
            last_sample = now
            if progress is not None:
                try:
                    progress(now - reset_mono, st)
                except Exception:  # noqa: BLE001
                    pass

    if aborted:
        reason = "aborted"
    elif st["first_fix_mono"] is not None and reason == "timeout_no_fix":
        reason = "timeout_with_fix"

    configure_sentences(ser)

    with open(samples_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(samples[0].keys()) if samples else ["elapsed_s"])
        w.writeheader()
        w.writerows(samples)

    with open(events_path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    valid_rows = [r for r in samples if r["fix_valid"]]
    first_fix_elapsed = next((r["elapsed_s"] for r in valid_rows), None)
    post_rows = [r for r in samples
                 if first_fix_elapsed is not None and r["elapsed_s"] >= first_fix_elapsed]
    sats_used = [r["sats_used"] for r in valid_rows if r["sats_used"] is not None]
    sats_view = [r["sats_in_view"] for r in samples if r["sats_in_view"] is not None]
    snrs = [r["best_snr"] for r in samples if r["best_snr"] is not None]
    hdops = [r["hdop"] for r in valid_rows if r["hdop"] is not None]
    stale = [r["sec_since_last_fix"] for r in samples
             if r["sec_since_last_fix"] is not None]

    ttff = (round(st["first_fix_mono"] - reset_mono, 2)
            if st["first_fix_mono"] is not None else None)
    nmea_gap = (round(first_after_reset["t"] - reset_mono, 2)
                if first_after_reset["t"] is not None else None)
    gap_cold = nmea_gap is not None and nmea_gap >= 2.0
    pi_cold = uptime_start is not None and uptime_start < 300
    if gap_cold:
        cold_basis = "ubx_reset_gap"
    elif pi_cold:
        cold_basis = "pi_power_cycle"
    else:
        cold_basis = "unverified"

    summary = {
        "trial": trial_no,
        "boot_id": boot_id(),
        "pi_uptime_at_start_s": uptime_start,
        "started_system_utc": samples[0]["system_utc"] if samples else None,
        "max_sec": max_sec,
        "post_fix_sec": post_fix_sec,
        "samples": len(samples),
        "valid_samples": len(valid_rows),
        "fix_valid_fraction": round(len(valid_rows) / len(samples), 3) if samples else None,
        "post_fix_samples": len(post_rows),
        "post_fix_fix_valid_fraction": (round(sum(1 for r in post_rows if r["fix_valid"]) / len(post_rows), 3)
                                        if post_rows else None),
        "ttff_s": ttff,
        "fix_losses": st["fix_losses"],
        "nmea_lines": st["nmea"], "gga_lines": st["gga"],
        "gsv_lines": st["gsv"], "rmc_lines": st["rmc"],
        "nmea_gap_after_reset_s": nmea_gap,
        "cold_start_observed": gap_cold or pi_cold,
        "cold_start_basis": cold_basis,
        "module": info,
        "sats_used_min": min(sats_used) if sats_used else None,
        "sats_used_median": med(sats_used),
        "sats_used_max": max(sats_used) if sats_used else None,
        "sats_in_view_median": med(sats_view),
        "sats_in_view_max": max(sats_view) if sats_view else None,
        "best_snr_median": med(snrs),
        "best_snr_max": max(snrs) if snrs else None,
        "hdop_median": med(hdops),
        "stale_age_max_s": max(stale) if stale else None,
        "stale_age_end_s": stale[-1] if stale else None,
        "temp_c_min": min(temps) if temps else None,
        "temp_c_max": max(temps) if temps else None,
        "throttled_end": throttled(),
        "completed_reason": reason,
        "complete": (aborted is None and reason in ("fix_hold_complete", "timeout_with_fix", "timeout_no_fix")),
        "aborted": aborted,
        "coords_retained": False,
    }
    write_json(os.path.join(out_dir, "summary.json"), summary)
    return summary


def append_index(run_dir, summary):
    idx = os.path.join(RUN_ROOT, "index.jsonl")
    try:
        with open(idx, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "run": os.path.basename(run_dir),
                "run_dir": run_dir,
                "trial": summary["trial"],
                "ttff_s": summary["ttff_s"],
                "fix_valid_fraction": summary["fix_valid_fraction"],
                "cold_start_observed": summary["cold_start_observed"],
                "complete": summary["complete"],
                "boot_id": summary.get("boot_id"),
                "system_utc": summary.get("started_system_utc"),
            }) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# boot mode
# --------------------------------------------------------------------------
def boot_once(args):
    state = read_state()
    if not state or not state.get("armed"):
        print("GPS trial: not armed; normal boot.")
        return 0
    n = int(state.get("next_trial", 1))
    target = int(state.get("target", 3))
    if n > target:
        clear_state(completed=True)
        print("GPS trial: campaign already complete; normal boot.")
        return 0

    delay = args.boot_delay if args.boot_delay is not None else state.get("boot_delay", 20)
    if delay:
        print(f"GPS trial: waiting {delay}s for the app to finish booting")
        time.sleep(delay)

    run_dir = state["run_dir"]
    out_dir = os.path.join(run_dir, f"trial_{n}")

    if disk_free_mb() < MIN_DISK_MB:
        print(f"GPS trial {n}: refusing to start, only {disk_free_mb()} MB free")
        return 0

    if os.path.exists(out_dir):
        # A previous attempt was interrupted before the state advanced.
        print(f"GPS trial: overwriting interrupted attempt in {out_dir}")
        for f in ("samples.csv", "events.jsonl", "summary.json"):
            try:
                os.remove(os.path.join(out_dir, f))
            except OSError:
                pass

    rc, _, _ = sudo(["systemctl", "stop", "transitguard"])
    time.sleep(2 if rc == 0 else 0)

    # The app owns the LCD while it is running, so this must happen after stop.
    lcd = lcd_open()
    lcd_show(lcd, f"T{n}/{target} GPS TEST", "starting...")

    t0 = temp_c()
    if t0 is None or t0 > MAX_TEMP_C:
        lcd_show(lcd, f"T{n} TOO HOT", f"{t0 if t0 is not None else '?'}C COOL")
        print(f"GPS trial {n}: refusing to start, temp={t0} (limit {MAX_TEMP_C})")
        return 1

    success = False
    summary = None
    ser = None
    try:
        ser = open_port()
        info = module_info(ser)
        print(f"module: {info}")

        post_fix = int(state.get("post_fix_sec", args.post_fix_sec))
        max_sec = int(state.get("max_sec", args.max_sec))

        def progress(elapsed, st):
            if st["first_fix_mono"] is None:
                lcd_show(lcd, f"T{n}/{target} GPS", f"waiting {int(elapsed)}s")
            elif not st["valid"]:
                lcd_show(lcd, f"T{n} FIX LOST", f"waiting {int(elapsed)}s")
            else:
                ttff = st["first_fix_mono"] - st["reset_mono"]
                lcd_show(lcd, f"T{n} FIX {ttff:.0f}s", f"hold {post_fix}s")

        summary = run_trial(ser, n, max_sec, post_fix, info, out_dir,
                            temp_c(), progress=progress)
        append_index(run_dir, summary)

        # Record the receiver identity in the campaign manifest too.
        manifest = read_json(os.path.join(run_dir, "manifest.json"), {})
        if manifest:
            manifest.setdefault("module", info)
            write_json(os.path.join(run_dir, "manifest.json"), manifest)

        if summary["complete"]:
            state["next_trial"] = n + 1
            state.setdefault("trials", []).append({
                "trial": n, "ttff_s": summary["ttff_s"],
                "fix_valid_fraction": summary["fix_valid_fraction"],
                "complete": True, "boot_id": summary.get("boot_id"),
                "system_utc": summary.get("started_system_utc"),
            })
            if n >= target:
                state["armed"] = False
                state["completed_utc"] = now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")
                write_json(DONE_PATH, state)
                try:
                    os.remove(STATE_PATH)
                except OSError:
                    pass
            else:
                write_state(state)

            write_json(os.path.join(run_dir, "run_summary.json"), {
                "run": os.path.basename(run_dir),
                "mode": "boot-one-trial-per-power-on",
                "updated_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "target": target,
                "next_trial": state["next_trial"],
                "armed": state["armed"],
                "trials": state.get("trials", []),
                "temp_c_end": temp_c(),
                "throttled_end": throttled(),
            })

            # The user may cut power as soon as the LCD says DONE: flush first.
            sync_disk()

            if summary["ttff_s"] is None:
                lcd_show(lcd, f"T{n} NO FIX", "POWER OFF")
            else:
                lcd_show(lcd, f"T{n} DONE {summary['ttff_s']:.0f}s",
                         "POWER OFF" if n < target else "ALL 3 DONE")
            beep(2)
            success = True
            print(f"GPS trial {n} complete: TTFF {summary['ttff_s']} s, "
                  f"fix {summary['fix_valid_fraction']}, sats {summary['sats_used_median']}")
            if n >= target:
                lcd_show(lcd, "ALL 3 DONE", "SSH TO PULL")
        else:
            sync_disk()
            lcd_show(lcd, f"T{n} ABORTED", "HOT? SEE LOG")
            print(f"GPS trial {n} aborted: {summary.get('aborted')}")
    except Exception as exc:  # noqa: BLE001
        print(f"GPS trial failed: {exc}")
        traceback.print_exc()
        sync_disk()
        lcd_show(lcd, f"T{n} ERROR", "SSH / LOGS")
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
        # transitguard is intentionally left stopped: the LCD keeps the result
        # visible and SSH stays available. A power cycle starts the next trial
        # (or a normal boot once the campaign is complete). On failure the same
        # trial is retried next boot because the state was not advanced.
        if not success:
            print("GPS trial did not complete; transitguard left stopped "
                  "(LCD shows the error, SSH remains available).")
    return 0 if success else 1


# --------------------------------------------------------------------------
# normal (all-in-one) mode
# --------------------------------------------------------------------------
def open_port():
    try:
        import serial  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "pyserial missing: run with /home/john/fatigue/fatigue-env/bin/python3")
    import serial
    last = None
    for _ in range(20):
        try:
            return serial.Serial(PORT, BAUD, timeout=0.5)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"could not open {PORT} at {BAUD}: {last}")


def cleanup_runtime(keep_service=False):
    if not keep_service:
        sudo(["systemctl", "start", "transitguard"])


def run_normal(args):
    t0 = temp_c()
    if t0 is None or t0 > MAX_TEMP_C:
        sys.exit(f"refusing to start: temp={t0} (limit {MAX_TEMP_C})")
    if t0 > args.max_start_temp:
        sys.exit(f"refusing to start: temp={t0} > --max-start-temp {args.max_start_temp}")
    if disk_free_mb() < MIN_DISK_MB:
        sys.exit(f"refusing to start: only {disk_free_mb()} MB free")
    if not os.path.exists(PORT):
        sys.exit(f"refusing to start: {PORT} does not exist")

    trial_sec = args.probe_sec if args.probe else int(args.max_sec)
    run_id = stamp()
    run_dir = os.path.join(PROBE_ROOT if args.probe else RUN_ROOT, run_id)
    os.makedirs(run_dir, exist_ok=False)
    manifest = {
        "run": run_id,
        "mode": "probe" if args.probe else "trials",
        "started_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "trials_planned": 1 if args.probe else args.trials,
        "max_sec": trial_sec,
        "post_fix_sec": args.post_fix_sec,
        "gap_sec": args.gap_sec,
        "port": PORT, "baud": BAUD,
        "app_head": sh(["git", "-C", APP, "rev-parse", "HEAD"])[1],
        "vehicle_monitor_sha256": sha256(os.path.join(APP, "vehicle_monitor.py")),
        "clock_sync": sh(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])[1],
        "utc_offset": dt.datetime.now().astimezone().strftime("%z"),
        "temp_c_start": t0,
        "throttled_start": throttled(),
        "disk_free_mb": disk_free_mb(),
        "cold_start_method": ("UBX-CFG-RST navBbrMask=0xFFFF "
                              "(receiver-level; no Pi reboot between trials)"),
        "coords_retained": False,
    }
    rc, ssid, _ = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    manifest["active_ssid"] = next((l.split(":", 1)[1] for l in ssid.splitlines()
                                    if l.startswith("yes:")), None)
    write_json(os.path.join(run_dir, "manifest.json"), manifest)

    print(f"=== GPS {'probe' if args.probe else 'trials'} {run_id}")
    print(f"port {PORT} @ {BAUD}, max {trial_sec}s, post-fix {args.post_fix_sec}s")

    sudo(["systemctl", "stop", "transitguard"])
    time.sleep(2)

    summaries = []
    ser = None
    try:
        ser = open_port()
        info = module_info(ser)
        print(f"module: {info}")
        manifest["module"] = info
        write_json(os.path.join(run_dir, "manifest.json"), manifest)

        for i in range(1, (1 if args.probe else args.trials) + 1):
            if _stop["flag"]:
                break
            temp = temp_c()
            if temp is not None and temp > MAX_TEMP_C:
                _stop.update(flag=True, reason=f"SoC {temp} C above {MAX_TEMP_C} C")
                break
            print(f"\n--- trial {i} starting at {now_utc().strftime('%H:%M:%SZ')} "
                  f"({temp} C)")
            s = run_trial(ser, i, trial_sec, args.post_fix_sec, info,
                          os.path.join(run_dir, f"trial_{i}"), temp)
            s["summary_dir"] = os.path.dirname(os.path.join(run_dir, f"trial_{i}"))
            append_index(run_dir, s)
            summaries.append(s)
            print(f"    TTFF {s['ttff_s']} s · fix-valid {s['fix_valid_fraction']} · "
                  f"sats used {s['sats_used_median']} · "
                  f"cold-start {s['cold_start_observed']} · "
                  f"reason {s['completed_reason']}")
            if s["aborted"]:
                break
            if i < (1 if args.probe else args.trials) and not args.probe:
                print(f"    cooling {args.gap_sec}s")
                time.sleep(args.gap_sec)
    finally:
        if ser is not None:
            try:
                ser.close()
            except Exception:  # noqa: BLE001
                pass
        cleanup_runtime(args.keep_service)
        write_json(os.path.join(run_dir, "run_summary.json"), {
            "run": run_id,
            "finished_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "trials": summaries,
            "temp_c_end": temp_c(),
            "throttled_end": throttled(),
        })
        print(f"\n=== done: {run_dir}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boot", action="store_true",
                    help="run one armed boot trial and leave the result on the LCD")
    ap.add_argument("--arm", action="store_true", help="create a campaign and arm it")
    ap.add_argument("--disarm", action="store_true", help="cancel a remaining campaign")
    ap.add_argument("--install", action="store_true", help="install/enable the boot unit")
    ap.add_argument("--start-now", action="store_true",
                    help="after arming, start the boot unit immediately (no extra reboot)")
    ap.add_argument("--target", type=int, default=3, help="trials per campaign (default 3)")
    ap.add_argument("--max-sec", type=int, default=180,
                    help="hard cap per trial (default 180 s = 3 min)")
    ap.add_argument("--post-fix-sec", type=int, default=30,
                    help="stop after holding a fix this long (default 30 s)")
    ap.add_argument("--boot-delay", type=float, default=None,
                    help="seconds to let the app boot before stopping it (arm-time, default 20)")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--trial-min", type=float, default=None,
                    help="legacy: fixed trial length in minutes (overrides --max-sec)")
    ap.add_argument("--gap-sec", type=int, default=120)
    ap.add_argument("--probe", action="store_true",
                    help="one short probe (default 90 s) to identify the module/reset")
    ap.add_argument("--probe-sec", type=int, default=90)
    ap.add_argument("--max-start-temp", type=float, default=75.0)
    ap.add_argument("--keep-service", action="store_true")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, lambda *_: _stop.update(flag=True, reason="SIGINT"))
    signal.signal(signal.SIGTERM, lambda *_: _stop.update(flag=True, reason="SIGTERM"))

    if args.trial_min is not None:
        args.max_sec = int(args.trial_min * 60)
    if args.boot_delay is None:
        args.boot_delay = 20

    if args.install:
        install_unit(delay=args.boot_delay)
        if not (args.arm or args.boot):
            return
    if args.disarm:
        clear_state(completed=True)
        print("disarmed")
        return
    if args.arm:
        arm(args)
        if args.start_now:
            sudo(["systemctl", "start", "--no-block", "tg-gps-trial.service"])
            print("boot trial started now (watch the LCD)")
        return
    if args.boot:
        sys.exit(boot_once(args))
    run_normal(args)


if __name__ == "__main__":
    main()
