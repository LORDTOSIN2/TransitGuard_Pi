#!/usr/bin/env python3
"""
TransitGuard integrated outdoor test.

One run = three short outdoor sessions (default 150 s each) while the normal
TransitGuard app keeps running.  The harness polls the app's local /data
endpoint once per second, so the dashboard (:8080) remains available and the
app keeps owning the camera, GPS serial port, MQ-3/ADS1115 and LCD.

What it records
---------------
* GPS: fix_valid, latitude/longitude, satellites used/in view, HDOP, altitude,
  speed, location age and a simple distance travelled from fresh fixes.
* Fatigue: face_present/frontal, EAR, MAR, PERCLOS, pitch and driver_state.
* Alcohol: ratio, estimated ppm and the app's alert/caution thresholds.
* System: SoC temperature, throttling, network buffer, telemetry queue.
* Packet delivery: with --trace (default), installs a temporary systemd
  drop-in setting TELEMETRY_TRACE_PATH, so the app writes per-message
  scheduled/attempted/dropped events with message IDs and HTTP status.  The
  local trace is summarized here; tools/analyze_integrated_test.py can compare
  the message IDs with Supabase rows.

The local-only app endpoint POST /test/lcd is used for session progress.  It
does not stop the app and does not bypass the normal driver-state LCD messages;
the harness refreshes the overlay every few seconds.

Typical manual run
------------------
    cd /home/john/fatigue
    ./fatigue-env/bin/python3 tools/run_integrated_test.py \
        --sessions 3 --session-sec 150 --gap-sec 30

Boot-campaign workflow (recommended for "power on then record")
--------------------------------------------------------------
    ./fatigue-env/bin/python3 tools/run_integrated_test.py --install
    ./fatigue-env/bin/python3 tools/run_integrated_test.py --arm \
        --sessions 3 --session-sec 150 --gap-sec 30
    # power-cycle, carry the Pi outside, wait for the sessions
    ./fatigue-env/bin/python3 tools/run_integrated_test.py --disarm   # if needed

The unit is enabled but does nothing on normal boots unless BOOT_STATE.json
exists.  At the end of the three sessions it writes BOOT_STATE.done.json and
removes the armed state, so later power cycles are normal.  The app is left
running and the dashboard remains available.

Artifacts (unique run folder under logs/integrated/)
----------------------------------------------------
    manifest.json       app/config/host/network/plan
    preflight.json      safety readings before the run
    phases.json         pre/session/gap/post UTC boundaries
    samples.csv         1 Hz GPS/fatigue/alcohol/system samples
    telemetry_trace.jsonl  app scheduled/attempt/dropped telemetry events
    delivery_local.json local trace counts and per-session breakdown
    summary.json        per-session summaries and non-claims
    postflight.json     service/data/trace cleanup state
    harness.log         not written by default (systemd/journal or tee stdout)

Non-claims
----------
This is an end-to-end functional/availability run, not a position-accuracy,
fatigue-accuracy, alcohol-concentration or fleet-reliability experiment.  It
does not induce or certify fatigue, and the MQ-3 ratio is a relative vapor
signal, not BAC/BrAC.  GPS fixes are recorded, not surveyed ground truth.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import math
import os
import signal
import statistics as stats
import subprocess
import sys
import time
import urllib.error
import urllib.request

APP = "/home/john/fatigue"
LOGS = os.path.join(APP, "logs")
RUN_ROOT = os.path.join(LOGS, "integrated")
STATE_PATH = os.path.join(RUN_ROOT, "BOOT_STATE.json")
DONE_PATH = os.path.join(RUN_ROOT, "BOOT_STATE.done.json")
UNIT_PATH = "/etc/systemd/system/tg-integrated-test.service"
TRACE_DROPIN = "/etc/systemd/system/transitguard.service.d/zz-integrated-trace.conf"
DATA_URL = "http://127.0.0.1:8080/data"
LCD_URL = "http://127.0.0.1:8080/test/lcd"
MAX_TEMP_C = 86.0
MIN_DISK_MB = 100.0
LCD_COLS = 16

_stop = {"flag": False, "reason": None}


# ---------------------------------------------------------------------------
# small utilities
# ---------------------------------------------------------------------------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def utc_iso(d=None):
    return (d or now_utc()).strftime("%Y-%m-%dT%H:%M:%SZ")


def sh(cmd, check=False, timeout=30):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", "command not found"
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd!r} failed ({p.returncode}): {p.stderr.strip()[:300]}")
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def sudo(cmd, check=False, timeout=90):
    return sh(["sudo", "-n"] + cmd, check=check, timeout=timeout)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


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
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def boot_id():
    return read_text("/proc/sys/kernel/random/boot_id").strip() or None


def uptime_s():
    try:
        return float(read_text("/proc/uptime").split()[0])
    except (ValueError, IndexError):
        return None


def active_ssid():
    rc, out, _ = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    return next((line.split(":", 1)[1] for line in out.splitlines()
                 if line.startswith("yes:")), None)


def ip_address():
    rc, out, _ = sh(["hostname", "-I"])
    return out.split()[0] if out else None


def get_json(url, timeout=4):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return {}


def post_json(url, obj, timeout=3):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def wait_heartbeat(timeout=120):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline and not _stop["flag"]:
        d = get_json(DATA_URL)
        updated = d.get("updated_at")
        if updated and updated != last:
            return d
        last = updated
        time.sleep(1.5)
    return {}


def median(xs):
    return round(stats.median(xs), 3) if xs else None


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def _truthy(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes")


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _ascii16(text):
    text = str(text).replace("\n", " ").replace("\r", " ")
    text = "".join(c if 32 <= ord(c) < 127 else "?" for c in text)
    return text[:LCD_COLS]


# ---------------------------------------------------------------------------
# LCD overlay (local app endpoint)
# ---------------------------------------------------------------------------
class LcdOverlay:
    def __init__(self, enabled=True):
        self._default_enabled = bool(enabled)
        self.enabled = bool(enabled)
        self.last = None
        self.failures = 0
        self.last_error = None
        self.ok_once = False

    def reset(self):
        self.enabled = self._default_enabled
        self.last = None
        self.failures = 0

    def show(self, line1, line2, force=False):
        if not self.enabled:
            return False
        line1, line2 = _ascii16(line1), _ascii16(line2)
        text = (line1, line2)
        if not force and text == self.last:
            return True
        try:
            status, body = post_json(LCD_URL, {"line1": line1, "line2": line2})
            if status != 200 or not body.get("ok"):
                raise RuntimeError(f"HTTP {status}: {body}")
            self.last = text
            self.failures = 0
            self.ok_once = True
            return True
        except Exception as exc:  # noqa: BLE001
            self.failures += 1
            self.last_error = str(exc)
            if self.failures in (1, 3):
                print(f"LCD overlay unavailable ({self.failures}x): {exc}")
            if self.failures >= 3:
                self.enabled = False
            return False


# ---------------------------------------------------------------------------
# trace drop-in
# ---------------------------------------------------------------------------
def install_trace(trace_path):
    os.makedirs(os.path.dirname(TRACE_DROPIN), exist_ok=True)
    body = "[Service]\nEnvironment=TELEMETRY_TRACE_PATH=%s\n" % trace_path
    p = subprocess.run(["sudo", "-n", "tee", TRACE_DROPIN], input=body, text=True,
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"could not write trace drop-in: {p.stderr.strip()}")
    sudo(["systemctl", "daemon-reload"], check=True)
    sudo(["systemctl", "restart", "transitguard"], check=True)
    d = wait_heartbeat(150)
    if not d:
        raise RuntimeError("app did not answer /data after enabling telemetry trace")
    return d


def remove_trace():
    sudo(["rm", "-f", TRACE_DROPIN])
    sudo(["systemctl", "daemon-reload"])
    sudo(["systemctl", "reset-failed", "transitguard"])
    sudo(["systemctl", "restart", "transitguard"])
    return wait_heartbeat(150)


def service_active():
    return sh(["systemctl", "is-active", "transitguard"])[1]


def app_manifest():
    env = {}
    for line in read_text("/etc/transitguard.env").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    secret = ("TOKEN", "PASSWORD", "KEY", "SECRET")
    safe_env = {k: ("<redacted>" if any(s in k.upper() for s in secret) else v)
                for k, v in env.items()}
    return {
        "app_head": sh(["git", "-C", APP, "rev-parse", "HEAD"])[1],
        "app_git_dirty": bool(sh(["git", "-C", APP, "status", "--porcelain"])[1]),
        "vehicle_monitor_sha256": sha256(os.path.join(APP, "vehicle_monitor.py")),
        "model_sha256": sha256(os.path.join(APP, "face_landmarker.task")),
        "config_keys": sorted(env.keys()),
        "config_values": safe_env,
        "boot_id": boot_id(),
        "uptime_s_at_start": uptime_s(),
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "python_version": sys.version.split()[0],
        "clock_sync": sh(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])[1],
        "utc_offset": now_utc().astimezone().strftime("%z"),
        "active_ssid": active_ssid(),
        "ip_address": ip_address(),
    }


# ---------------------------------------------------------------------------
# recorder
# ---------------------------------------------------------------------------
FIELDS = [
    "timestamp_utc", "wall_epoch", "phase", "session", "phase_elapsed_s",
    "driver_state", "face_present", "frontal", "ear", "mar", "perclos", "pitch",
    "latitude", "longitude", "fix_valid", "using_last_known", "location_age_sec",
    "sats_used", "sats_in_view", "best_snr", "fix_quality", "hdop",
    "altitude_m", "speed_kmh", "alcohol_ratio", "alcohol_ppm",
    "alert_threshold", "caution_threshold", "network_online", "buffer_count",
    "telemetry_queue_size", "alert_coalesced", "pi_temperature", "pi_throttled",
    "pi_temp_level", "buzzer_active", "lcd_message", "test_injected_state",
    "source_updated_at", "fetch_ok", "error",
]


class Recorder:
    def __init__(self, path):
        self.path = path
        self.fh = open(path, "w", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.fh, fieldnames=FIELDS)
        self.writer.writeheader()
        self.fh.flush()
        self.rows = []
        self.last = {}
        self.fetch_failures = 0

    def sample(self, phase, session, phase_start_epoch):
        d = get_json(DATA_URL)
        ok = 1 if d else 0
        if not d:
            self.fetch_failures += 1
        else:
            self.fetch_failures = 0
            self.last = d
        row = {
            "timestamp_utc": utc_iso(),
            "wall_epoch": round(time.time(), 3),
            "phase": phase,
            "session": session,
            "phase_elapsed_s": round(time.time() - phase_start_epoch, 2),
            "driver_state": d.get("driver_state"),
            "face_present": d.get("face_present"),
            "frontal": d.get("frontal"),
            "ear": d.get("ear"),
            "mar": d.get("mar"),
            "perclos": d.get("perclos"),
            "pitch": d.get("pitch"),
            "latitude": d.get("latitude"),
            "longitude": d.get("longitude"),
            "fix_valid": d.get("fix_valid"),
            "using_last_known": d.get("using_last_known"),
            "location_age_sec": d.get("location_age_sec"),
            "sats_used": d.get("sats_used"),
            "sats_in_view": d.get("sats_in_view"),
            "best_snr": d.get("best_snr"),
            "fix_quality": d.get("fix_quality"),
            "hdop": d.get("hdop"),
            "altitude_m": d.get("altitude_m"),
            "speed_kmh": d.get("speed_kmh"),
            "alcohol_ratio": d.get("alcohol_ratio"),
            "alcohol_ppm": d.get("alcohol_ppm"),
            "alert_threshold": d.get("alcohol_alert_threshold"),
            "caution_threshold": d.get("alcohol_caution_threshold"),
            "network_online": d.get("network_online"),
            "buffer_count": d.get("buffer_count"),
            "telemetry_queue_size": d.get("telemetry_queue_size"),
            "alert_coalesced": d.get("alert_coalesced"),
            "pi_temperature": d.get("pi_temperature"),
            "pi_throttled": d.get("pi_throttled"),
            "pi_temp_level": d.get("pi_temp_level"),
            "buzzer_active": d.get("buzzer_active"),
            "lcd_message": d.get("lcd_message"),
            "test_injected_state": d.get("test_injected_state"),
            "source_updated_at": d.get("updated_at"),
            "fetch_ok": ok,
            "error": "" if ok else "/data unavailable",
        }
        self.writer.writerow(row)
        self.fh.flush()
        self.rows.append(row)
        return row

    def close(self):
        try:
            self.fh.flush()
            os.fsync(self.fh.fileno())
        except OSError:
            pass
        self.fh.close()


# ---------------------------------------------------------------------------
# phase execution
# ---------------------------------------------------------------------------
def _lcd_for_phase(lcd, phase, session, total, remaining, d):
    if phase.startswith("pre"):
        line1 = f"PRE {int(remaining):3d}s"
        line2 = "WAIT FOR GPS"
    elif phase.startswith("gap"):
        line1 = f"NEXT {session}/{total} {int(remaining):3d}s"
        line2 = "PREP / WALK"
    elif phase.startswith("post"):
        line1 = "TEST COMPLETE"
        line2 = "PACKING UP"
    else:
        sats = d.get("sats_used")
        fix = d.get("fix_valid")
        gps = f"GPS{int(sats)}" if (fix and sats is not None) else "GPSNO"
        line1 = f"T{session}/{total} {int(remaining):3d}s {gps}"
        ratio = d.get("alcohol_ratio")
        alc = f"{ratio:.1f}" if isinstance(ratio, (int, float)) else "--"
        face = "FACE" if d.get("face_present") else "NOFACE"
        line2 = f"ALC {alc} {face}"
    lcd.show(line1, line2)


def run_phase(recorder, lcd, phases, phases_path, name, session, duration,
              total, args):
    start = now_utc()
    start_epoch = time.time()
    end_epoch = start_epoch + duration
    phase = {
        "name": name,
        "session": session,
        "start_utc": utc_iso(start),
        "end_utc": utc_iso(start + dt.timedelta(seconds=duration)),
        "start_epoch": round(start_epoch, 3),
        "end_epoch": round(end_epoch, 3),
        "duration_sec": duration,
        "actual_end_utc": None,
        "actual_end_epoch": None,
    }
    phases.append(phase)
    write_json(phases_path, {"phases": phases})
    print(f"\n>>> {name.upper()} {session}/{total} {duration}s  "
          f"{phase['start_utc']} -> {phase['end_utc']}")
    lcd.show(f"T{session}/{total} START" if session else "PRE START",
             "WALK + RECORD" if session else "WAIT FOR GPS", force=True)

    next_sample = start_epoch
    last_lcd = 0.0
    last_temp = 0.0
    try:
        while time.time() < end_epoch and not _stop["flag"]:
            now = time.time()
            if now >= next_sample:
                recorder.sample(name, session, start_epoch)
                next_sample += 1.0
                if next_sample < now:
                    next_sample = now + 1.0
                if recorder.fetch_failures >= 10:
                    raise RuntimeError("app /data unavailable for 10 consecutive samples")
            if now - last_temp >= 5.0:
                t = temp_c()
                if t is not None and t > args.max_temp:
                    _stop["flag"] = True
                    _stop["reason"] = f"SoC {t} C above {args.max_temp} C"
                last_temp = now
            if now - last_lcd >= 5.0:
                _lcd_for_phase(lcd, name, session, total,
                               max(0, end_epoch - now), recorder.last)
                last_lcd = now
            time.sleep(0.2)
    finally:
        phase["actual_end_utc"] = utc_iso()
        phase["actual_end_epoch"] = round(time.time(), 3)
        write_json(phases_path, {"phases": phases})
    if _stop["flag"]:
        print(f"stopped: {_stop['reason']}")
    return phase


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------
def _window(rows, start, end):
    return [r for r in rows if start <= _float(r.get("wall_epoch") or 0) < end]


def summarize_rows(rows, start, end):
    win = _window(rows, start, end)
    n = len(win)
    if not n:
        return {"samples": 0}
    fresh = []
    for r in win:
        lat, lon = _float(r.get("latitude")), _float(r.get("longitude"))
        if (_truthy(r.get("fix_valid")) and not _truthy(r.get("using_last_known"))
                and lat is not None and lon is not None):
            fresh.append((_float(r.get("wall_epoch")), lat, lon,
                          r.get("timestamp_utc")))
    distance = 0.0
    for a, b in zip(fresh, fresh[1:]):
        distance += haversine_m(a[1], a[2], b[1], b[2])
    ratios = [(_float(r.get("alcohol_ratio")), r.get("timestamp_utc"))
              for r in win if _float(r.get("alcohol_ratio")) is not None]
    ratio_vals = [x[0] for x in ratios]
    ratio_min = min(ratios, key=lambda x: x[0]) if ratios else (None, None)
    alert_thr = next((_float(r.get("alert_threshold")) for r in win
                      if _float(r.get("alert_threshold")) is not None), None)
    caution_thr = next((_float(r.get("caution_threshold")) for r in win
                        if _float(r.get("caution_threshold")) is not None), None)
    states = {}
    for r in win:
        states[str(r.get("driver_state"))] = states.get(str(r.get("driver_state")), 0) + 1
    sats = [int(x) for x in (_int(r.get("sats_used")) for r in win) if x is not None]
    hdops = [x for x in (_float(r.get("hdop")) for r in win) if x is not None]
    ages = [x for x in (_float(r.get("location_age_sec")) for r in win) if x is not None]
    ears = [x for x in (_float(r.get("ear")) for r in win) if x is not None]
    mars = [x for x in (_float(r.get("mar")) for r in win) if x is not None]
    perclos = [x for x in (_float(r.get("perclos")) for r in win) if x is not None]
    temps = [x for x in (_float(r.get("pi_temperature")) for r in win) if x is not None]
    buffers = [int(x) for x in (_int(r.get("buffer_count")) for r in win) if x is not None]
    queues = [int(x) for x in (_int(r.get("telemetry_queue_size")) for r in win) if x is not None]
    face = sum(1 for r in win if _truthy(r.get("face_present")))
    frontal = sum(1 for r in win if _truthy(r.get("frontal")))
    offline = sum(1 for r in win if r.get("network_online") is False)
    below_alert = sum(1 for v in ratio_vals if alert_thr is not None and v < alert_thr)
    below_caution = sum(1 for v in ratio_vals if caution_thr is not None and v < caution_thr)
    return {
        "samples": n,
        "gps_fresh_fix_pct": round(100.0 * len(fresh) / n, 1),
        "gps_fix_first_utc": fresh[0][3] if fresh else None,
        "gps_fix_last_utc": fresh[-1][3] if fresh else None,
        "gps_distance_m": round(distance, 1) if fresh else None,
        "gps_sats_used_median": median(sats),
        "gps_hdop_median": median(hdops),
        "gps_location_age_s_median": median(ages),
        "face_present_pct": round(100.0 * face / n, 1),
        "frontal_pct": round(100.0 * frontal / n, 1),
        "ear_median": median(ears),
        "mar_median": median(mars),
        "perclos_median": median(perclos),
        "driver_states": states,
        "alcohol_ratio_n": len(ratio_vals),
        "alcohol_ratio_median": median(ratio_vals),
        "alcohol_ratio_min": ratio_min[0],
        "alcohol_ratio_min_utc": ratio_min[1],
        "alcohol_below_alert_samples": below_alert,
        "alcohol_below_caution_samples": below_caution,
        "alcohol_alert_threshold": alert_thr,
        "alcohol_caution_threshold": caution_thr,
        "network_offline_samples": offline,
        "buffer_count_max": max(buffers) if buffers else None,
        "telemetry_queue_max": max(queues) if queues else None,
        "pi_temp_min": min(temps) if temps else None,
        "pi_temp_max": max(temps) if temps else None,
    }


def read_trace(path):
    out = []
    if not path or not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return out


def trace_summary(events, phases, start_epoch, end_epoch):
    events = [e for e in events
              if start_epoch - 2 <= _float(e.get("wall_epoch") or 0) <= end_epoch + 30]
    scheduled = [e for e in events if e.get("ev") == "scheduled"]
    attempts = [e for e in events if e.get("ev") == "attempt"]
    dropped = [e for e in events if e.get("ev") == "dropped"]
    sched_ids = {e.get("messageId") for e in scheduled if e.get("messageId")}
    att_ids = {e.get("messageId") for e in attempts if e.get("messageId")}
    ok_attempts = [e for e in attempts if e.get("ok") and e.get("http_status") == 201]
    ok_ids = {e.get("messageId") for e in ok_attempts if e.get("messageId")}
    statuses = {}
    for e in attempts:
        statuses[str(e.get("http_status"))] = statuses.get(str(e.get("http_status")), 0) + 1
    rtts = [_float(e.get("rtt_ms")) for e in attempts if _float(e.get("rtt_ms")) is not None]
    by_session = {}
    for p in phases:
        if not p.get("session"):
            continue
        a, b = p["start_epoch"], p["end_epoch"]
        ss = [e for e in scheduled if a <= _float(e.get("wall_epoch") or 0) < b]
        aa = [e for e in attempts if a <= _float(e.get("wall_epoch") or 0) < b]
        by_session[str(p["session"])] = {
            "phase": p["name"],
            "scheduled": len(ss),
            "scheduled_unique": len({e.get("messageId") for e in ss if e.get("messageId")}),
            "attempts": len(aa),
            "attempt_ok_201": sum(1 for e in aa if e.get("ok") and e.get("http_status") == 201),
            "attempt_failed": sum(1 for e in aa if not (e.get("ok") and e.get("http_status") == 201)),
        }
    return {
        "enabled": bool(events),
        "events": len(events),
        "scheduled": len(scheduled),
        "scheduled_unique": len(sched_ids),
        "attempts": len(attempts),
        "attempted_unique": len(att_ids),
        "attempt_ok_201": len(ok_attempts),
        "attempt_ok_unique": len(ok_ids),
        "attempt_failed": len(attempts) - len(ok_attempts),
        "http_statuses": statuses,
        "rtt_ms_median": median(rtts),
        "dropped": len(dropped),
        "by_session": by_session,
        "scheduled_not_attempted": len(sched_ids - att_ids),
        "attempted_not_ok": len(att_ids - ok_ids),
    }


# ---------------------------------------------------------------------------
# preflight / campaign
# ---------------------------------------------------------------------------
def preflight(args):
    temp = temp_c()
    pre = {
        "started_utc": utc_iso(),
        "temp_c": temp,
        "throttled": throttled(),
        "disk_free_mb": disk_free_mb(),
        "service_active": service_active(),
        "camera_devices": sorted(p for p in os.listdir("/dev") if p.startswith("video")),
        "active_ssid": active_ssid(),
        "ip_address": ip_address(),
        "boot_id": boot_id(),
        "uptime_s": uptime_s(),
    }
    if temp is None or temp > MAX_TEMP_C:
        raise RuntimeError(f"refusing to start: temp={temp} C (hard limit {MAX_TEMP_C})")
    if temp > args.max_start_temp:
        raise RuntimeError(f"refusing to start: temp={temp} C > --max-start-temp {args.max_start_temp}")
    if pre["disk_free_mb"] < MIN_DISK_MB:
        raise RuntimeError(f"refusing to start: only {pre['disk_free_mb']} MB free")
    if pre["service_active"] != "active":
        raise RuntimeError("refusing to start: transitguard is not active")
    if not pre["camera_devices"]:
        raise RuntimeError("refusing to start: no /dev/video* camera device")
    d = wait_heartbeat(120)
    if not d:
        raise RuntimeError("app did not answer /data")
    if d.get("test_injected_state"):
        raise RuntimeError("test-state injection is active; disable it before a measured run")
    pre["heartbeat"] = {
        "updated_at": d.get("updated_at"),
        "driver_state": d.get("driver_state"),
        "alcohol_ratio": d.get("alcohol_ratio"),
        "fix_valid": d.get("fix_valid"),
    }
    return pre, d


def new_run_dir(label=""):
    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    suffix = ""
    if label:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in label)[:40]
        suffix = "_" + safe
    run_dir = os.path.join(RUN_ROOT, f"run_{stamp}{suffix}")
    os.makedirs(run_dir, exist_ok=False)
    return run_dir


def plan_from_args(args):
    return {
        "sessions": args.sessions,
        "session_sec": args.session_sec,
        "gap_sec": args.gap_sec,
        "pre_sec": args.pre_sec,
        "post_sec": args.post_sec,
        "boot_delay": args.boot_delay,
        "trace": not args.no_trace,
        "lcd": not args.no_lcd,
        "label": args.label,
        "max_start_temp": args.max_start_temp,
        "max_temp": args.max_temp,
    }


def run_campaign(cfg, run_dir, mode):
    os.makedirs(run_dir, exist_ok=True)
    started = now_utc()
    print(f"=== integrated test {run_dir} ({mode}) {utc_iso(started)}")
    print(f"plan: {cfg}")

    preflight_info = None
    manifest = None
    trace_path = os.path.join(run_dir, "telemetry_trace.jsonl") if cfg["trace"] else None
    phases = []
    phases_path = os.path.join(run_dir, "phases.json")
    recorder = None
    trace_installed = False
    aborted = None
    lcd = LcdOverlay(enabled=bool(cfg["lcd"]))
    try:
        preflight_info, hb = preflight(_ArgsView(cfg))
        write_json(os.path.join(run_dir, "preflight.json"), preflight_info)
        manifest = app_manifest()
        manifest.update({
            "run": os.path.basename(run_dir),
            "mode": mode,
            "started_utc": utc_iso(started),
            "plan": cfg,
            "trace_path": trace_path,
            "trace_enabled": bool(trace_path),
            "coords_retained": True,
            "video_retained": False,
            "metrics_only": True,
            "note": "End-to-end availability/function run; no accuracy or concentration claim.",
        })
        write_json(os.path.join(run_dir, "manifest.json"), manifest)

        lcd.show("INTEGRATED TEST", "WAIT APP", force=True)
        if trace_path:
            print("enabling telemetry trace and restarting app once...")
            trace_installed = True  # cleanup even if the restart fails partway
            hb = install_trace(trace_path)
            lcd.reset()
            wait_heartbeat(30)
        lcd.reset()
        lcd.show("INTEGRATED TEST", "STARTING", force=True)
        print(f"app ready: state={hb.get('driver_state')} alc={hb.get('alcohol_ratio')} "
              f"fix={hb.get('fix_valid')}")

        recorder = Recorder(os.path.join(run_dir, "samples.csv"))
        run_start_epoch = time.time()

        if cfg["pre_sec"]:
            run_phase(recorder, lcd, phases, phases_path, "pre", 0,
                      cfg["pre_sec"], cfg["sessions"], _ArgsView(cfg))
        for n in range(1, cfg["sessions"] + 1):
            if _stop["flag"]:
                break
            if n > 1 and cfg["gap_sec"]:
                run_phase(recorder, lcd, phases, phases_path, f"gap_{n}", n,
                          cfg["gap_sec"], cfg["sessions"], _ArgsView(cfg))
            if _stop["flag"]:
                break
            run_phase(recorder, lcd, phases, phases_path, f"session_{n}", n,
                      cfg["session_sec"], cfg["sessions"], _ArgsView(cfg))
        if not _stop["flag"] and cfg["post_sec"]:
            run_phase(recorder, lcd, phases, phases_path, "post", 0,
                      cfg["post_sec"], cfg["sessions"], _ArgsView(cfg))
        if _stop["flag"]:
            aborted = _stop["reason"] or "stopped"

        # Give the uploader a moment to drain the last queued message.
        time.sleep(10)
    except Exception as exc:  # noqa: BLE001
        aborted = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {aborted}")
        import traceback
        traceback.print_exc()
    finally:
        if recorder is not None:
            recorder.close()
        run_end_epoch = time.time()
        if trace_installed:
            try:
                print("restoring app without telemetry trace...")
                remove_trace()
            except Exception as exc:  # noqa: BLE001
                print(f"trace cleanup failed: {exc}")
        # A final overlay is best-effort after the app has restarted.
        try:
            lcd.reset()
            lcd.show(f"{cfg['sessions']}/{cfg['sessions']} DONE" if not aborted else "TEST ABORTED",
                     "PULL LOGS" if not aborted else "SEE JOURNAL", force=True)
        except Exception:
            pass

        rows = recorder.rows if recorder is not None else []
        session_summary = {}
        for p in phases:
            if p.get("session"):
                session_summary[str(p["session"])] = summarize_rows(
                    rows, p["start_epoch"], p["actual_end_epoch"] or p["end_epoch"])
        overall = summarize_rows(rows, 0, float("inf"))
        events = read_trace(trace_path) if trace_path else []
        trace = trace_summary(events, phases,
                              phases[0]["start_epoch"] if phases else 0.0,
                              run_end_epoch)
        write_json(os.path.join(run_dir, "delivery_local.json"), {
            **trace,
            "trace_path": trace_path,
            "supabase_checked": False,
            "note": ("HTTP 201 means the Edge Function accepted the message; "
                     "it is not proof of a Supabase row. Run "
                     "tools/analyze_integrated_test.py with Supabase URL/key "
                     "to match message IDs against public.telemetry."),
        })
        post = {
            "finished_utc": utc_iso(),
            "service_active": service_active(),
            "data_updated_at": get_json(DATA_URL).get("updated_at"),
            "temp_c": temp_c(),
            "throttled": throttled(),
            "disk_free_mb": disk_free_mb(),
            "trace_dropin_exists": os.path.exists(TRACE_DROPIN),
            "lcd_endpoint_ok": lcd.ok_once,
            "lcd_error": lcd.last_error,
            "stop_reason": _stop["reason"],
            "aborted": aborted,
        }
        write_json(os.path.join(run_dir, "postflight.json"), post)
        summary = {
            "run": os.path.basename(run_dir),
            "mode": mode,
            "plan": cfg,
            "started_utc": utc_iso(started),
            "finished_utc": utc_iso(),
            "aborted": aborted,
            "sessions": session_summary,
            "overall": overall,
            "telemetry_trace": trace,
            "coords_retained": True,
            "video_retained": False,
            "non_claims": [
                "relative MQ-3 vapor response, not BAC/BrAC or calibrated ppm",
                "fatigue metrics are not a clinical drowsiness diagnosis or accuracy claim",
                "GPS is recorded as reported; no surveyed position-accuracy claim",
                "packet delivery is a local trace/HTTP check; Supabase matching is separate",
                "three sessions in one run are not a fleet or long-term reliability claim",
            ],
        }
        write_json(os.path.join(run_dir, "summary.json"), summary)

        print("\n=== per session")
        for k, v in session_summary.items():
            print(f"  session {k}: n={v.get('samples')} gps={v.get('gps_fresh_fix_pct')}% "
                  f"sats={v.get('gps_sats_used_median')} alc_min={v.get('alcohol_ratio_min')} "
                  f"face={v.get('face_present_pct')}% temp={v.get('pi_temp_min')}-{v.get('pi_temp_max')}")
        print(f"=== trace: scheduled={trace.get('scheduled')} "
              f"ok201={trace.get('attempt_ok_201')} failed={trace.get('attempt_failed')} "
              f"dropped={trace.get('dropped')}")
        print(f"=== aborted: {aborted}")
        print(f"=== artifacts: {run_dir}")

    return 1 if aborted else 0


class _ArgsView:
    """Small attribute view so helpers can read config dicts."""
    def __init__(self, cfg):
        self.max_temp = cfg["max_temp"]
        self.max_start_temp = cfg["max_start_temp"]


# ---------------------------------------------------------------------------
# boot / unit
# ---------------------------------------------------------------------------
UNIT_TEMPLATE = """[Unit]
Description=TransitGuard integrated outdoor test (one boot, 3 sessions)
After=transitguard.service network-online.target
Wants=transitguard.service network-online.target
ConditionPathExists={state}

[Service]
Type=oneshot
User=john
Group=john
SupplementaryGroups=dialout gpio
WorkingDirectory={app}
Environment=PYTHONUNBUFFERED=1
ExecStart={python} {app}/tools/run_integrated_test.py --boot
TimeoutStartSec=1800
Nice=5

[Install]
WantedBy=multi-user.target
"""


def install_unit():
    unit = UNIT_TEMPLATE.format(state=STATE_PATH, app=APP,
                                python=os.path.join(APP, "fatigue-env/bin/python3"))
    p = subprocess.run(["sudo", "-n", "tee", UNIT_PATH], input=unit, text=True,
                       capture_output=True)
    if p.returncode != 0:
        sys.exit(f"could not write {UNIT_PATH}: {p.stderr.strip()}")
    sudo(["systemctl", "daemon-reload"], check=True)
    rc, out, err = sudo(["systemctl", "enable", "tg-integrated-test.service"])
    if rc != 0:
        sys.exit(f"could not enable unit: {err}")
    print(f"installed and enabled {UNIT_PATH}")
    print("Unit is inert until --arm creates BOOT_STATE.json.")


def arm(cfg):
    if os.path.exists(STATE_PATH):
        sys.exit(f"{STATE_PATH} already exists; run --disarm first")
    if not os.path.exists(UNIT_PATH):
        print("warning: unit not installed; run --install before power-cycling")
    run_dir = new_run_dir(cfg["label"])
    state = {
        "armed": True,
        "mode": "integrated-one-boot",
        "run_dir": run_dir,
        "plan": cfg,
        "armed_utc": utc_iso(),
    }
    write_json(STATE_PATH, state)
    write_json(os.path.join(run_dir, "armed.json"), state)
    print(f"armed: {run_dir}")
    print(f"power-cycle now. Boot runs {cfg['sessions']} sessions x "
          f"{cfg['session_sec']}s, gap {cfg['gap_sec']}s, boot delay {cfg['boot_delay']}s.")
    print("The LCD shows the session and final result; the dashboard remains available.")
    return 0


def boot_campaign():
    state = read_json(STATE_PATH)
    if not state or not state.get("armed"):
        print("integrated test: not armed; normal boot.")
        return 0
    cfg = state["plan"]
    delay = int(cfg.get("boot_delay", 0))
    if delay:
        print(f"integrated test: waiting {delay}s for boot/app stabilisation")
        time.sleep(delay)
    rc = run_campaign(cfg, state["run_dir"], "boot")
    if rc == 0:
        state["armed"] = False
        state["completed_utc"] = utc_iso()
        write_json(DONE_PATH, state)
        try:
            os.remove(STATE_PATH)
        except OSError:
            pass
        print("integrated test: campaign complete; disarmed for future boots.")
    else:
        print("integrated test: campaign failed; state left armed for a retry.")
    return rc


def disarm():
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)
        print(f"removed {STATE_PATH}")
    else:
        print("no armed integrated-test state")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--boot", action="store_true", help="run an armed boot campaign")
    ap.add_argument("--install", action="store_true", help="install/enable the boot unit")
    ap.add_argument("--arm", action="store_true", help="arm the next boot")
    ap.add_argument("--disarm", action="store_true", help="remove the armed state")
    ap.add_argument("--sessions", type=int, default=3)
    ap.add_argument("--session-sec", type=int, default=150,
                    help="seconds per session (default 150; 2-3 min)")
    ap.add_argument("--gap-sec", type=int, default=30)
    ap.add_argument("--pre-sec", type=int, default=20)
    ap.add_argument("--post-sec", type=int, default=10)
    ap.add_argument("--boot-delay", type=int, default=20)
    ap.add_argument("--max-start-temp", type=float, default=80.0)
    ap.add_argument("--max-temp", type=float, default=MAX_TEMP_C)
    ap.add_argument("--no-trace", action="store_true",
                    help="do not restart the app to enable telemetry tracing")
    ap.add_argument("--no-lcd", action="store_true")
    ap.add_argument("--label", default="")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, lambda s, f: _stop.update(flag=True, reason=f"signal {s}"))
    signal.signal(signal.SIGTERM, lambda s, f: _stop.update(flag=True, reason=f"signal {s}"))

    if args.install:
        install_unit()
        return 0
    if args.arm:
        if args.sessions < 1 or args.session_sec < 30:
            sys.exit("invalid plan: need >=1 session and >=30 s per session")
        return arm(plan_from_args(args))
    if args.disarm:
        return disarm()
    if args.boot:
        return boot_campaign()

    if os.path.exists(STATE_PATH):
        sys.exit(f"{STATE_PATH} is armed; use --boot/--disarm or remove it before a manual run")
    if args.sessions < 1 or args.session_sec < 30:
        sys.exit("invalid plan: need >=1 session and >=30 s per session")
    cfg = plan_from_args(args)
    run_dir = new_run_dir(cfg["label"])
    return run_campaign(cfg, run_dir, "manual")


if __name__ == "__main__":
    sys.exit(main())
