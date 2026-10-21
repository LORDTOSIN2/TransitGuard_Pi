#!/usr/bin/env python3
"""
TransitGuard post-mitigation Wi-Fi/HTTPS measurement session harness.

Runs ON the Raspberry Pi (python3, stdlib only). One invocation = one session.

What it does
------------
1. Safety preflight (refuses to start above 86 C, low disk, wrong hotspot,
   stale heartbeat, non-empty offline buffer unless explicitly allowed) and
   writes the full software/test manifest.
2. Creates a unique, never-overwritten run folder under logs/runs/.
3. Installs a temporary systemd drop-in that enables telemetry tracing into
   that folder and the gated scripted-state input, then restarts the service.
4. Keeps the vehicle stationary and routine telemetry at the configured
   cadence, sampling /data every 10 s (temperature, throttling, buffers,
   queue depth, GPS availability, camera-metric availability, Wi-Fi).
5. Drives a short, scripted alert/state-flap interval (states written to a
   file -- never fatigue actions in front of a driver) and logs every write.
6. Stops on schedule or immediately if the SoC exceeds 86 C.
7. Postflight readings, removes the drop-in, restarts, verifies the trace and
   test-input variables are gone, and writes summary.json.

Teardown is guaranteed by try/finally + SIGINT/SIGTERM handling + a
systemd-run deadman that removes the drop-in if this process dies.

Usage (on the Pi)
-----------------
    python3 tools/run_session.py --session 1 --minutes 30 \
        --hotspot "SFM MBB - CONF"

    # dry run: 3 minutes, short flap window, no Supabase reconciliation
    python3 tools/run_session.py --session 0 --minutes 3 --hotspot "SFM MBB - CONF"

Artifacts (per run folder, unique names)
----------------------------------------
    manifest.json        software/test manifest + preflight network state
    preflight.json       safety readings before the test
    postflight.json      safety readings after the test (+ after restart)
    samples.csv          10 s poll during the session
    flaps.csv            every scripted state write (epoch + state)
    telemetry_trace.jsonl raw scheduled/attempt/dropped trace (written by app)
    summary.json         session counts, queue high-water, cadence, RTT stats
    harness.log          full stdout/stderr of this harness

Reconciliation against Supabase is done afterwards by
tools/analyze_campaign.py (which needs network access, not necessarily the Pi).
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
import urllib.error
import urllib.request

APP = "/home/john/fatigue"
LOGS = os.path.join(APP, "logs")
RUN_ROOT = os.path.join(LOGS, "runs")
DROPIN = "/etc/systemd/system/transitguard.service.d/zz-eval-session.conf"
STATE_FILE = os.path.join(LOGS, "eval_state.txt")
DATA_URL = "http://127.0.0.1:8080/data"
MAX_TEMP_C = 86.0            # hard safety limit from the test plan
MIN_DISK_MB = 100            # tracing + logs are small, but never start desperate
SAMPLE_SEC = 10.0

FLAP_STATES = ["CRITICAL_FATIGUE", "NORMAL", "DROWSY", "NORMAL",
               "CRITICAL_FATIGUE", "DROWSY", "NORMAL"]

_stop = {"flag": False, "reason": None}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(d=None):
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


def sudo(cmd, check=False, timeout=60):
    return sh(["sudo", "-n"] + cmd, check=check, timeout=timeout)


def read_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return ""


def read_kv(path):
    out = {}
    for line in read_text(path).splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def sha256(path):
    h = __import__("hashlib").sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def temp_c():
    try:
        return round(int(read_text("/sys/class/thermal/thermal_zone0/temp").strip()) / 1000.0, 1)
    except (ValueError, OSError):
        return None


def throttled():
    rc, out, _ = sh("vcgencmd get_throttled")
    val = out.split("=", 1)[1].strip() if "=" in out else None
    return val


def decode_throttled(value):
    """Human-readable interpretation of get_throttled bit flags."""
    if not value:
        return {"raw": value, "notes": []}
    try:
        v = int(value, 16)
    except ValueError:
        return {"raw": value, "notes": ["unparseable"]}
    notes = []
    for bit, label in (
        (0x1, "under-voltage NOW"), (0x2, "arm frequency capped NOW"),
        (0x4, "currently throttled"), (0x8, "soft temperature limit NOW"),
        (0x10000, "under-voltage has occurred"), (0x20000, "arm frequency capping has occurred"),
        (0x40000, "throttling has occurred"), (0x80000, "soft temperature limit has occurred"),
    ):
        if v & bit:
            notes.append(label)
    return {"raw": value, "bits_set": notes, "clean": v == 0}


def mem_available_mb():
    for line in read_text("/proc/meminfo").splitlines():
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1024.0, 1)
    return None


def disk_free_mb(path="/"):
    try:
        st = os.statvfs(path)
        return round(st.f_bavail * st.f_frsize / (1024.0 * 1024.0), 1)
    except OSError:
        return None


def current_ssid():
    rc, out, _ = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    for line in out.splitlines():
        if line.startswith("yes:"):
            return line.split(":", 1)[1].replace("\\:", ":").strip()
    return None


def wifi_rssi():
    for line in read_text("/proc/net/wireless").splitlines():
        if line.lstrip().startswith("wlan0:"):
            fields = line.split(":", 1)[1].split()
            if len(fields) >= 3:
                try:
                    return int(float(fields[2].rstrip(".")))
                except ValueError:
                    return None
    return None


def local_ip():
    rc, out, _ = sh(["hostname", "-I"])
    return out.split()[0] if out else None


def get_json(url, timeout=6):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def ingest_reachable(url):
    """Any HTTP answer (even 401/405) proves the ingest host is reachable."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return {"reachable": True, "http_status": resp.status}
    except urllib.error.HTTPError as exc:
        return {"reachable": True, "http_status": exc.code}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": str(exc)[:200]}


def wait_heartbeat(timeout=150, label="startup"):
    """Wait until /data reports a fresh, changing updated_at."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline and not _stop["flag"]:
        d = get_json(DATA_URL)
        if d and d.get("updated_at"):
            if last is not None and d["updated_at"] != last:
                return d
            last = d["updated_at"]
        time.sleep(3)
    raise RuntimeError(f"{label}: no live heartbeat from /data within {timeout}s "
                       "(camera/main-loop problem; restart the service again)")


# --------------------------------------------------------------------------
# safety preflight / manifest
# --------------------------------------------------------------------------
def safety_readings():
    th = throttled()
    uptime = read_text("/proc/uptime").split()
    return {
        "captured_utc": iso(),
        "temp_c": temp_c(),
        "temp_limit_c": MAX_TEMP_C,
        "throttled_hex": th,
        "throttled_decoded": decode_throttled(th),
        "disk_free_mb": disk_free_mb("/"),
        "mem_available_mb": mem_available_mb(),
        "buffer_count": (get_json(DATA_URL) or {}).get("buffer_count"),
        "uptime_s": round(float(uptime[0]), 1) if uptime else None,
    }


def clock_status():
    rc, out, _ = sh(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "Timezone",
                     "-p", "TimeUSec"])
    parsed = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    rc2, tracking, _ = sh(["chronyc", "tracking"])
    return {
        "ntp_synchronized": parsed.get("NTPSynchronized"),
        "timezone": parsed.get("Timezone"),
        "utc_now": iso(),
        "local_now": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "offset_from_utc_s": round(dt.datetime.now().astimezone().utcoffset().total_seconds(), 1),
        "chronyc_tracking": tracking or None,
    }


def software_manifest():
    rc, head, _ = sh(["git", "-C", APP, "rev-parse", "HEAD"])
    rc, status, _ = sh(["git", "-C", APP, "status", "--porcelain"])
    env = {}
    for key, val in read_kv("/etc/transitguard.env").items():
        # Never record secrets or anything that looks like one.
        if any(s in key for s in ("TOKEN", "PASSWORD", "KEY", "SECRET")):
            env[key] = "<redacted>"
        else:
            env[key] = val
    os_release = read_kv("/etc/os-release")
    model = read_text("/proc/device-tree/model").strip("\x00\r\n ") or None
    rc, pip_freeze, _ = sh([os.path.join(APP, "fatigue-env/bin/python3"), "-m", "pip",
                            "freeze", "--disable-pip-version-check"], timeout=90)
    rc, pyver, _ = sh([os.path.join(APP, "fatigue-env/bin/python3"), "--version"])
    return {
        "app_git_head": head,
        "app_git_dirty": bool(status.strip()),
        "app_git_status": status.splitlines(),
        "app_file_sha256": sha256(os.path.join(APP, "vehicle_monitor.py")),
        "model_file": "face_landmarker.task",
        "model_sha256": sha256(os.path.join(APP, "face_landmarker.task")),
        "config_sha256": sha256("/etc/transitguard.env"),
        "config_keys": env,
        "os_pretty_name": os_release.get("PRETTY_NAME"),
        "kernel": read_text("/proc/sys/kernel/osrelease").strip() or None,
        "hardware_model": model,
        "cpu_count": os.cpu_count(),
        "python": pyver,
        "python_executable": os.path.join(APP, "fatigue-env/bin/python3"),
        "packages": pip_freeze.splitlines(),
        "systemd_unit": "transitguard.service",
    }


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# systemd drop-in / teardown
# --------------------------------------------------------------------------
def install_dropin(trace_path):
    sudo(["mkdir", "-p", os.path.dirname(DROPIN)], check=True)
    body = (
        "[Service]\n"
        f"Environment=TELEMETRY_TRACE_PATH={trace_path}\n"
        "Environment=ENABLE_TEST_STATE_INPUT=1\n"
        f"Environment=TEST_STATE_INPUT_PATH={STATE_FILE}\n"
    )
    p = subprocess.run(["sudo", "-n", "tee", DROPIN], input=body, text=True,
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"cannot write drop-in: {p.stderr.strip()[:200]}")
    sudo(["systemctl", "daemon-reload"], check=True)


def remove_dropin():
    sudo(["rm", "-f", DROPIN])
    sudo(["systemctl", "daemon-reload"])
    sudo(["systemctl", "reset-failed", "transitguard"])


def service_env_has(needle):
    rc, out, _ = sh(["systemctl", "show", "transitguard", "-p", "Environment"])
    return needle in out


def arm_deadman(dead_after_s):
    """Remove the drop-in even if this harness process is killed."""
    cmd = (f"sleep {int(dead_after_s)}; rm -f {DROPIN}; systemctl daemon-reload; "
           "systemctl restart transitguard; logger -t tg-eval 'deadman: tracing/test input cleared'")
    sudo(["systemctl", "stop", "tg-eval-deadman.service"], timeout=20)
    sudo(["systemd-run", "--collect", "--unit=tg-eval-deadman", "/bin/sh", "-c", cmd],
         timeout=20)


def disarm_deadman():
    sudo(["systemctl", "stop", "tg-eval-deadman.service"], timeout=20)
    sudo(["systemctl", "reset-failed", "tg-eval-deadman.service"], timeout=20)


def restart_and_wait(trace_path, label):
    sudo(["systemctl", "restart", "transitguard"], check=True, timeout=90)
    data = wait_heartbeat(timeout=180, label=label)
    if "telemetry_queue_size" not in data:
        raise RuntimeError(
            "running app does not expose telemetry_queue_size -- the deployed "
            "vehicle_monitor.py is not the version this harness expects")
    if not service_env_has(trace_path):
        raise RuntimeError("TELEMETRY_TRACE_PATH is not visible to the running unit")
    return data


# --------------------------------------------------------------------------
# session
# --------------------------------------------------------------------------
def trace_stats(trace_path):
    events = []
    if os.path.exists(trace_path):
        with open(trace_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    sched = [e for e in events if e.get("ev") == "scheduled"]
    att = [e for e in events if e.get("ev") == "attempt"]
    drops = [e for e in events if e.get("ev") == "dropped"]
    rtts = [a["rtt_ms"] for a in att if a.get("rtt_ms") is not None]
    queued_ts = sorted(s["wall_epoch"] for s in sched if "wall_epoch" in s)
    gaps = [b - a for a, b in zip(queued_ts, queued_ts[1:]) if 0 < b - a < 120]

    def pctl(vals, p):
        if not vals:
            return None
        vs = sorted(vals)
        k = (len(vs) - 1) * p / 100.0
        lo, hi = int(k), min(int(k) + 1, len(vs) - 1)
        return round(vs[lo] + (vs[hi] - vs[lo]) * (k - lo), 1)

    return {
        "trace_events": len(events),
        "scheduled": len(sched),
        "dropped": len(drops),
        "attempted": len(att),
        "attempted_unique": len({a.get("messageId") for a in att if a.get("messageId")}),
        "accepted_http201": sum(1 for a in att if a.get("http_status") == 201),
        "failed": sum(1 for a in att if a.get("http_status") != 201),
        "queue_high_water_trace": max((s.get("queue_size") or 0) for s in sched) if sched else 0,
        "cadence_median_s": round(statistics.median(gaps), 2) if gaps else None,
        "cadence_min_s": round(min(gaps), 2) if gaps else None,
        "cadence_max_s": round(max(gaps), 2) if gaps else None,
        "rtt_ms": {
            "n": len(rtts),
            "min": round(min(rtts), 1) if rtts else None,
            "median": round(statistics.median(rtts), 1) if rtts else None,
            "mean": round(statistics.mean(rtts), 1) if rtts else None,
            "p90": pctl(rtts, 90),
            "p95": pctl(rtts, 95),
            "p99": pctl(rtts, 99),
            "stdev": round(statistics.stdev(rtts), 1) if len(rtts) > 1 else None,
            "max": round(max(rtts), 1) if rtts else None,
        },
    }


def sample_row(phase, elapsed, session_started):
    d = get_json(DATA_URL) or {}
    inj = d.get("test_injected_state")
    return {
        "t_epoch": round(time.time(), 3),
        "t_iso": iso(),
        "elapsed_s": round(elapsed, 1),
        "phase": phase,
        "temp_c": temp_c(),
        "throttled": throttled(),
        "buffer_count": d.get("buffer_count"),
        "telemetry_queue_size": d.get("telemetry_queue_size"),
        "network_online": d.get("network_online"),
        "driver_state": d.get("driver_state"),
        "injected_state": inj,
        "ssid": current_ssid(),
        "rssi_dbm": wifi_rssi(),
        "ear_present": d.get("ear") is not None,
        "mar_present": d.get("mar") is not None,
        "perclos_present": d.get("perclos") is not None,
        "pitch_present": d.get("pitch") is not None,
        "fix_valid": d.get("fix_valid"),
        "using_last_known": d.get("using_last_known"),
        "location_age_sec": d.get("location_age_sec"),
        "sats_used": d.get("sats_used"),
        "sats_in_view": d.get("sats_in_view"),
        "best_snr": d.get("best_snr"),
        "fix_quality": d.get("fix_quality"),
        "heartbeat": d.get("updated_at"),
        "disk_free_mb": disk_free_mb("/"),
    }


def wait_for_cooldown(max_temp, max_wait_min, log):
    """Wait (bounded) for the SoC to fall to max_temp before a session.

    Returns (record, temp_now). The record is written into the manifest so a
    start that needed a cool-down is visible in the results."""
    started = time.time()
    trail = []
    while True:
        t = temp_c()
        trail.append({"t": iso(), "temp_c": t})
        if t is None or t <= max_temp:
            return {"target_c": max_temp, "max_wait_min": max_wait_min,
                    "waited_s": round(time.time() - started, 1), "reached": True,
                    "samples": trail}, t
        if time.time() - started >= max_wait_min * 60:
            return {"target_c": max_temp, "max_wait_min": max_wait_min,
                    "waited_s": round(time.time() - started, 1), "reached": False,
                    "samples": trail}, t
        log(f"cool-down: {t} C is above {max_temp} C; waiting before the run")
        time.sleep(20)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", type=int, default=1)
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--hotspot", default="SFM MBB - CONF",
                    help="required active SSID; the run aborts on a mismatch")
    ap.add_argument("--flap-start-min", type=float, default=10.0)
    ap.add_argument("--flap-minutes", type=float, default=3.0)
    ap.add_argument("--flap-period-s", type=float, default=20.0)
    ap.add_argument("--allow-nonempty-buffer", action="store_true")
    ap.add_argument("--allow-unsynced-clock", action="store_true")
    ap.add_argument("--max-start-temp", type=float, default=80.0,
                    help="wait for the SoC to fall to this temperature before starting")
    ap.add_argument("--cool-wait-min", type=float, default=10.0,
                    help="maximum cool-down wait before refusing to start")
    ap.add_argument("--skip-restart", action="store_true",
                    help="smoke-test only: do not touch systemd (uses current env)")
    args = ap.parse_args()

    os.makedirs(RUN_ROOT, exist_ok=True)
    run_id = f"{now_utc():%Y%m%dT%H%M%SZ}_s{args.session}"
    run_dir = os.path.join(RUN_ROOT, run_id)
    if os.path.exists(run_dir):
        print(f"refusing to reuse existing run folder {run_dir}")
        return 2
    os.makedirs(run_dir)
    trace_path = os.path.join(run_dir, "telemetry_trace.jsonl")
    log_path = os.path.join(run_dir, "harness.log")
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg):
        line = f"[{iso()}] {msg}"
        print(line)
        log_fh.write(line + "\n")

    def on_signal(signum, _frame):
        _stop["flag"] = True
        _stop["reason"] = f"signal {signum}"
        log(f"caught signal {signum}; will abort after current step")

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGHUP, on_signal)   # survive an SSH drop, then tear down

    log(f"run_id={run_id} session={args.session} minutes={args.minutes} "
        f"hotspot={args.hotspot!r}")

    cooldown, t_now = wait_for_cooldown(args.max_start_temp, args.cool_wait_min, log)
    pre = safety_readings()
    clock = clock_status()
    net = {
        "ssid": current_ssid(),
        "rssi_dbm": wifi_rssi(),
        "ip": local_ip(),
        "ingest": ingest_reachable("https://hfzqfcuezuxcgaifxqok.supabase.co/rest/v1/"),
    }
    manifest = {
        "run_id": run_id,
        "session": args.session,
        "started_utc": iso(),
        "timezone": clock["timezone"],
        "clock": clock,
        "network_at_start": net,
        "software": software_manifest(),
        "plan": {
            "duration_min": args.minutes,
            "sample_sec": SAMPLE_SEC,
            "flap_start_min": args.flap_start_min,
            "flap_minutes": args.flap_minutes,
            "flap_period_s": args.flap_period_s,
            "flap_sequence": FLAP_STATES,
            "required_hotspot": args.hotspot,
            "routine_cadence_sec": 10,
            "vehicle_stationary": True,
            "test_input": "scripted state file (not fatigue actions)",
        },
        "preflight": pre,
        "cooldown": cooldown,
    }
    write_json(os.path.join(run_dir, "preflight.json"), pre)
    write_json(os.path.join(run_dir, "manifest.json"), manifest)

    # ---------------- preflight refusals ----------------
    refusals, warnings = [], []
    if pre["temp_c"] is None:
        refusals.append("temperature unreadable")
    elif pre["temp_c"] > MAX_TEMP_C:
        refusals.append(f"SoC {pre['temp_c']} C exceeds the {MAX_TEMP_C} C limit")
    elif pre["temp_c"] >= 85.0:
        warnings.append(f"SoC {pre['temp_c']} C is at/over the 85 C soft limit")
    if not cooldown["reached"] and (pre["temp_c"] or 0) > args.max_start_temp:
        refusals.append(f"SoC stayed at {pre['temp_c']} C, above the "
                        f"{args.max_start_temp} C start limit, after a "
                        f"{args.cool_wait_min:.0f} min cool-down wait")
    if (pre["disk_free_mb"] or 0) < MIN_DISK_MB:
        refusals.append(f"only {pre['disk_free_mb']} MB free")
    if clock.get("ntp_synchronized") != "yes" and not args.allow_unsynced_clock:
        refusals.append("clock is not NTP-synchronised (use --allow-unsynced-clock "
                        "only if you will record the skew limitation)")
    if net["ssid"] != args.hotspot:
        refusals.append(f"active SSID is {net['ssid']!r}, expected {args.hotspot!r}")
    if not net["ingest"].get("reachable"):
        refusals.append(f"ingest host unreachable: {net['ingest'].get('error')}")
    hb = get_json(DATA_URL)
    if not hb or not hb.get("updated_at"):
        refusals.append("/data has no live heartbeat (main loop not producing telemetry)")
    if hb and (hb.get("buffer_count") or 0) > 0 and not args.allow_nonempty_buffer:
        refusals.append(f"offline buffer already holds {hb.get('buffer_count')} packets")

    manifest["preflight_warnings"] = warnings
    if refusals:
        manifest["refused"] = refusals
        write_json(os.path.join(run_dir, "manifest.json"), manifest)
        for r in refusals:
            log(f"REFUSED: {r}")
        write_json(os.path.join(run_dir, "summary.json"),
                   {"run_id": run_id, "aborted": True, "abort_reason": "preflight",
                    "refusals": refusals})
        log("preflight refused the run; nothing was restarted")
        log_fh.close()
        return 3
    for w in warnings:
        log(f"WARNING: {w}")

    samples_path = os.path.join(run_dir, "samples.csv")
    flaps_path = os.path.join(run_dir, "flaps.csv")
    samples_fh = open(samples_path, "w", newline="", encoding="utf-8")
    writer = None

    started = now_utc()
    session_started = time.time()
    duration_s = args.minutes * 60.0
    flap_start_s = args.flap_start_min * 60.0
    flap_len_s = args.flap_minutes * 60.0
    phase = "baseline"
    flap_rows = []
    aborted = False
    reason = None

    try:
        if not args.skip_restart:
            log("installing trace/test-input drop-in and restarting transitguard")
            install_dropin(trace_path)
            arm_deadman(duration_s + 900)
            restart_and_wait(trace_path, "post-restart")
        else:
            log("--skip-restart: using the currently running configuration")

        # first sample row also gives us the post-restart readings
        first = sample_row("start", 0.0, started)
        writer = csv.DictWriter(samples_fh, fieldnames=list(first.keys()))
        writer.writeheader()
        writer.writerow(first)
        samples_fh.flush()

        log(f"session window {iso(started)} -> +{args.minutes:.1f} min; "
            f"flap at +{args.flap_start_min:.1f} min for {args.flap_minutes:.1f} min")

        next_sample = session_started
        flap_idx = 0
        next_flap = session_started + flap_start_s

        while True:
            elapsed = time.time() - session_started
            if _stop["flag"]:
                aborted, reason = True, _stop["reason"]
                break
            if elapsed >= duration_s:
                break

            # ---- scripted state window ----
            if elapsed >= flap_start_s and elapsed < flap_start_s + flap_len_s:
                if phase != "flap":
                    phase = "flap"
                    log("flap window open")
                if time.time() >= next_flap:
                    state = FLAP_STATES[flap_idx % len(FLAP_STATES)]
                    flap_idx += 1
                    try:
                        with open(STATE_FILE, "w", encoding="utf-8") as fh:
                            fh.write(state + "\n")
                        rec = {"t_epoch": round(time.time(), 3), "t_iso": iso(),
                               "elapsed_s": round(elapsed, 1), "state": state}
                        flap_rows.append(rec)
                        with open(flaps_path, "w", newline="", encoding="utf-8") as ff:
                            w = csv.DictWriter(ff, fieldnames=list(rec.keys()))
                            w.writeheader()
                            w.writerows(flap_rows)
                        log(f"flap -> {state}")
                    except OSError as exc:
                        log(f"WARNING: cannot write scripted state: {exc}")
                    next_flap += args.flap_period_s
            elif phase == "flap":
                phase = "recovery"
                # Release the override entirely so the app returns to real
                # detection; an injected NORMAL would silently mask a genuine
                # alert for the rest of the session.
                try:
                    os.remove(STATE_FILE)
                except OSError:
                    pass
                rec = {"t_epoch": round(time.time(), 3), "t_iso": iso(),
                       "elapsed_s": round(elapsed, 1), "state": "RELEASED"}
                flap_rows.append(rec)
                with open(flaps_path, "w", newline="", encoding="utf-8") as ff:
                    w = csv.DictWriter(ff, fieldnames=list(rec.keys()))
                    w.writeheader()
                    w.writerows(flap_rows)
                log("flap window closed -> override released")

            if time.time() >= next_sample:
                row = sample_row(phase, elapsed, started)
                writer.writerow(row)
                samples_fh.flush()
                t = row["temp_c"]
                if t is not None and t > MAX_TEMP_C:
                    aborted, reason = True, f"SoC {t} C exceeded {MAX_TEMP_C} C"
                    log(f"ABORT: {reason}")
                    break
                next_sample += SAMPLE_SEC

            time.sleep(0.5)

        elapsed_end = time.time() - session_started
        # final in-session sample
        try:
            final = sample_row("end", elapsed_end, started)
            writer.writerow(final)
            samples_fh.flush()
        except Exception:  # noqa: BLE001
            pass

    except Exception as exc:  # noqa: BLE001
        aborted = True
        reason = f"{type(exc).__name__}: {exc}"
        log(f"ERROR: {reason}")

    finally:
        # ---------------- teardown (always) ----------------
        try:
            samples_fh.close()
        except Exception:  # noqa: BLE001
            pass
        post = safety_readings()
        write_json(os.path.join(run_dir, "postflight.json"), post)
        if not args.skip_restart:
            log("teardown: removing drop-in and restarting the service")
            try:
                remove_dropin()
                disarm_deadman()
                # remove the scripted input so it cannot influence later runs
                try:
                    os.remove(STATE_FILE)
                except OSError:
                    pass
                after = wait_heartbeat(timeout=180, label="post-teardown")
                still_traced = service_env_has(trace_path)
                still_test_input = service_env_has("ENABLE_TEST_STATE_INPUT")
                log(f"teardown: trace var still set={still_traced} "
                    f"test-input var still set={still_test_input}")
                post["after_restart"] = {
                    "heartbeat": after.get("updated_at"),
                    "trace_env_present": still_traced,
                    "test_input_env_present": still_test_input,
                    "readings": safety_readings(),
                }
                write_json(os.path.join(run_dir, "postflight.json"), post)
                if still_traced or still_test_input:
                    log("WARNING: teardown left evaluation env vars set")
            except Exception as exc:  # noqa: BLE001
                log(f"TEARDOWN ERROR: {exc} -- remove {DROPIN} manually")

        summary = {
            "run_id": run_id,
            "session": args.session,
            "aborted": aborted,
            "abort_reason": reason,
            "started_utc": iso(started),
            "ended_utc": iso(),
            "duration_actual_min": round((time.time() - session_started) / 60.0, 2),
            "hotspot": net["ssid"],
            "rssi_dbm_start": net["rssi_dbm"],
            "flaps": flap_rows,
            "safety": {"pre": pre, "post": post},
        }
        summary.update(trace_stats(trace_path))
        # aggregate the samples for queue high-water and availability
        rows = []
        try:
            with open(samples_path, "r", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
        except OSError:
            pass
        if rows:
            def col(name):
                out = []
                for r in rows:
                    try:
                        out.append(float(r[name]))
                    except (TypeError, ValueError):
                        pass
                return out
            temps = col("temp_c")
            qs = col("telemetry_queue_size")
            bufs = col("buffer_count")
            summary["samples"] = {
                "n": len(rows),
                "temp_c_max": max(temps) if temps else None,
                "temp_c_mean": round(statistics.mean(temps), 1) if temps else None,
                "queue_high_water_samples": int(max(qs)) if qs else None,
                "buffer_high_water_samples": int(max(bufs)) if bufs else None,
                "heartbeat_missing_samples": sum(1 for r in rows if not r["heartbeat"]),
                "camera_ear_available_pct": round(
                    100.0 * sum(1 for r in rows if r["ear_present"] == "True") / len(rows), 1),
                "gps_fix_available_pct": round(
                    100.0 * sum(1 for r in rows if r["fix_valid"] == "True") / len(rows), 1),
                "ssids_seen": sorted({r["ssid"] for r in rows if r["ssid"]}),
                "throttled_values": sorted({r["throttled"] for r in rows if r["throttled"]}),
            }
            # first fix after start (time-to-first-fix, if one occurs)
            for r in rows:
                if r["fix_valid"] == "True":
                    summary["samples"]["first_fix_elapsed_s"] = float(r["elapsed_s"])
                    break
        write_json(os.path.join(run_dir, "summary.json"), summary)
        log(f"summary written: scheduled={summary.get('scheduled')} "
            f"attempted={summary.get('attempted')} dropped={summary.get('dropped')} "
            f"queue_hwm={summary.get('queue_high_water_trace')}")
        log_fh.close()

    return 0 if not aborted else 4


if __name__ == "__main__":
    sys.exit(main())
