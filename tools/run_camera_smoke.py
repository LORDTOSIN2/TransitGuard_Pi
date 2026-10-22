#!/usr/bin/env python3
"""
TransitGuard camera-pipeline availability smoke test.

Runs ON the Raspberry Pi (python3, stdlib only). One invocation = one short,
stationary, metrics-only test with a consented person in front of the camera.

What it does
------------
1. Safety preflight (refuses to start above 86 C or with low disk) and records
   the software/test manifest, camera device and clock-sync state.
2. Creates a unique run folder under logs/camera_smoke/<UTC>/.
3. Installs a temporary systemd drop-in that sets CAMERA_METRICS_LOG_PATH, then
   restarts the service. The app then writes one JSON line per second with
   face-present status, EAR/MAR/PERCLOS, head pose and classifier output.
   No frames, images or video are written.
4. Runs three announced phases: face present, face absent, face present again.
5. Postflight readings, removes the drop-in, restarts and verifies the variable
   is gone from the process environment.
6. Writes phases.json, samples.csv and summary.json.

The operator must be able to follow the phase announcements. The harness prints
them with UTC and local time and writes them to phases.json before each change.

Usage (on the Pi)
-----------------
    python3 tools/run_camera_smoke.py --present-sec 90 --absent-sec 60

Artifacts (unique per run)
--------------------------
    manifest.json    app/model hashes, camera device, clock sync, config keys
    preflight.json   safety readings before the test
    phases.json      UTC/local boundaries for present/absent phases
    samples.csv      10 s poll: temperature, throttle, app face state
    metrics.jsonl    one line per second from the app (metrics only)
    summary.json     per-phase availability, computed locally
    harness.log      full stdout/stderr

Analysis/reporting is done by tools/analyze_camera_smoke.py.
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
import urllib.request

APP = "/home/john/fatigue"
LOGS = os.path.join(APP, "logs")
RUN_ROOT = os.path.join(LOGS, "camera_smoke")
DROPIN = "/etc/systemd/system/transitguard.service.d/zz-camera-metrics.conf"
DATA_URL = "http://127.0.0.1:8080/data"
MAX_TEMP_C = 86.0
MIN_DISK_MB = 100
SAMPLE_SEC = 10.0

_stop = {"flag": False, "reason": None}


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso_local(d=None):
    return (d or dt.datetime.now().astimezone()).strftime("%Y-%m-%dT%H:%M:%S%z")


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


def redact_env(env):
    secret = ("TOKEN", "PASSWORD", "KEY", "SECRET")
    return {k: ("<redacted>" if any(s in k.upper() for s in secret) else v)
            for k, v in env.items()}


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


def service_pid():
    rc, out, _ = sh(["systemctl", "show", "transitguard", "-p", "MainPID", "--value"])
    try:
        return int(out)
    except ValueError:
        return 0


def process_has_env(pid, key):
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            return key.encode() in fh.read()
    except OSError:
        return None


def get_json(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def wait_heartbeat(timeout=60):
    """Wait until the app answers /data with a fresh heartbeat."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        d = get_json(DATA_URL)
        if d.get("updated_at") and d.get("updated_at") != last:
            return d
        last = d.get("updated_at")
        time.sleep(2)
    return {}


def wait_metrics(path, min_lines=5, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                if sum(1 for _ in fh) >= min_lines:
                    return True
        time.sleep(1)
    return False


def read_metrics(path):
    out = []
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


def summarize(rows, start, end):
    """Counts for the half-open window [start, end)."""
    win = [r for r in rows if start <= r.get("wall_epoch", 0) < end]
    n = len(win)
    if not n:
        return {"samples": 0}
    face = sum(1 for r in win if r.get("face_present"))
    front = sum(1 for r in win if r.get("frontal"))
    ear = [r["ear"] for r in win if r.get("ear") is not None]
    mar = [r["mar"] for r in win if r.get("mar") is not None]
    per = [r["perclos"] for r in win if r.get("perclos") is not None]
    fps = [r["fps"] for r in win if r.get("fps") is not None]
    states = {}
    for r in win:
        states[r.get("driver_state")] = states.get(r.get("driver_state"), 0) + 1

    def med(xs):
        return round(statistics.median(xs), 3) if xs else None

    return {
        "samples": n,
        "face_present_pct": round(100.0 * face / n, 1),
        "frontal_pct": round(100.0 * front / n, 1),
        "ear_available_pct": round(100.0 * len(ear) / n, 1),
        "mar_available_pct": round(100.0 * len(mar) / n, 1),
        "perclos_available_pct": round(100.0 * len(per) / n, 1),
        "ear_median": med(ear),
        "mar_median": med(mar),
        "perclos_median": med(per),
        "fps_median": med(fps),
        "fps_min": round(min(fps), 2) if fps else None,
        "driver_states": states,
    }


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)


def install_dropin(log_path):
    os.makedirs(os.path.dirname(DROPIN), exist_ok=True)
    body = "[Service]\nEnvironment=CAMERA_METRICS_LOG_PATH=%s\n" % log_path
    p = subprocess.run(["sudo", "-n", "tee", DROPIN], input=body, text=True,
                       capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"could not write drop-in: {p.stderr.strip()}")
    sudo(["systemctl", "daemon-reload"], check=True)
    sudo(["systemctl", "restart", "transitguard"], check=True)


def remove_dropin():
    sudo(["rm", "-f", DROPIN])
    sudo(["systemctl", "daemon-reload"])
    sudo(["systemctl", "reset-failed", "transitguard"])
    sudo(["systemctl", "restart", "transitguard"])


def on_signal(signum, frame):
    _stop["flag"] = True
    _stop["reason"] = f"signal {signum}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--present-sec", type=int, default=90,
                    help="duration of each face-present phase (default 90)")
    ap.add_argument("--absent-sec", type=int, default=60,
                    help="duration of the face-absent phase (default 60)")
    ap.add_argument("--max-start-temp", type=float, default=75.0,
                    help="refuse to start above this SoC temperature")
    ap.add_argument("--label", default="", help="optional run label")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    stamp = now_utc().strftime("%Y%m%dT%H%M%SZ")
    run_dir = os.path.join(RUN_ROOT, stamp + ("_" + args.label if args.label else ""))
    os.makedirs(run_dir, exist_ok=False)
    metrics_path = os.path.join(run_dir, "metrics.jsonl")

    started = now_utc()
    print(f"=== camera smoke {stamp} -> {run_dir}")
    print(f"phases: present {args.present_sec}s, absent {args.absent_sec}s, present {args.present_sec}s")

    # ---- preflight -----------------------------------------------------
    temp = temp_c()
    pre = {
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "temp_c": temp,
        "throttled": throttled(),
        "disk_free_mb": disk_free_mb(),
        "camera_devices": sorted(p for p in os.listdir("/dev") if p.startswith("video")),
        "service_active": sh(["systemctl", "is-active", "transitguard"])[1],
    }
    write_json(os.path.join(run_dir, "preflight.json"), pre)
    if temp is None or temp > MAX_TEMP_C:
        raise SystemExit(f"refusing to start: temp={temp} (limit {MAX_TEMP_C})")
    if temp > args.max_start_temp:
        raise SystemExit(f"refusing to start: temp={temp} > --max-start-temp {args.max_start_temp}")
    if pre["disk_free_mb"] < MIN_DISK_MB:
        raise SystemExit(f"refusing to start: only {pre['disk_free_mb']} MB free")
    if pre["service_active"] != "active":
        raise SystemExit("refusing to start: transitguard is not active")
    if not pre["camera_devices"]:
        raise SystemExit("refusing to start: no /dev/video* device")

    app_env = read_kv(os.path.join(APP, ".env"))
    manifest = {
        "run": stamp,
        "label": args.label,
        "started_utc": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "app_dir": APP,
        "app_head": sh(["git", "-C", APP, "rev-parse", "HEAD"])[1],
        "app_git_dirty": bool(sh(["git", "-C", APP, "status", "--porcelain"])[1]),
        "vehicle_monitor_sha256": sha256(os.path.join(APP, "vehicle_monitor.py")),
        "model_sha256": sha256(os.path.join(APP, "face_landmarker.task")),
        "config_keys": sorted(app_env.keys()),
        "config_values": redact_env(app_env),
        "clock_sync": sh(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])[1],
        "utc_offset": dt.datetime.now().astimezone().strftime("%z"),
        "hostname": os.uname().nodename,
        "kernel": os.uname().release,
        "python_version": sys.version.split()[0],
        "video_retained": False,
        "metrics_only": True,
    }
    rc, ssid, _ = sh(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"])
    manifest["active_ssid"] = next((l.split(":", 1)[1] for l in ssid.splitlines()
                                    if l.startswith("yes:")), None)
    write_json(os.path.join(run_dir, "manifest.json"), manifest)

    phases = []
    rows = []
    samples = []
    teardown_done = False

    try:
        install_dropin(metrics_path)
        d = wait_heartbeat(60)
        if not d:
            raise RuntimeError("app did not answer /data after restart")
        if not wait_metrics(metrics_path, 5, 60):
            raise RuntimeError("CAMERA_METRICS_LOG_PATH produced no lines")

        print(f"heartbeat {d.get('updated_at')} face_present={d.get('face_present')} "
              f"ear={d.get('ear')} state={d.get('driver_state')}")

        plan = [
            ("present_1", "STAY IN VIEW - face the camera, eyes open", args.present_sec),
            ("absent_1", "STEP OUT OF VIEW - leave the frame (or cover the lens)", args.absent_sec),
            ("present_2", "RETURN TO VIEW - face the camera again", args.present_sec),
        ]

        last_sample = 0.0
        for name, prompt, secs in plan:
            if _stop["flag"]:
                break
            start = now_utc()
            end = start + dt.timedelta(seconds=secs)
            phases.append({
                "name": name,
                "prompt": prompt,
                "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "start_epoch": round(start.timestamp(), 3),
                "end_epoch": round(end.timestamp(), 3),
                "duration_sec": secs,
                "local_start": start.astimezone().strftime("%H:%M:%S"),
            })
            write_json(os.path.join(run_dir, "phases.json"), {"phases": phases})
            print(f"\n>>> {name.upper()} from {start.strftime('%H:%M:%SZ')} "
                  f"to {end.strftime('%H:%M:%SZ')} ({secs}s): {prompt}\n")

            while now_utc() < end and not _stop["flag"]:
                t = time.time()
                if t - last_sample >= SAMPLE_SEC:
                    s = get_json(DATA_URL)
                    samples.append({
                        "wall_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
                        "phase": name,
                        "temp_c": temp_c(),
                        "throttled": throttled(),
                        "face_present": s.get("face_present"),
                        "ear": s.get("ear"),
                        "driver_state": s.get("driver_state"),
                        "buffer_count": s.get("buffer_count"),
                    })
                    last_sample = t
                    if temp_c() is not None and temp_c() > MAX_TEMP_C:
                        _stop["flag"] = True
                        _stop["reason"] = f"SoC {temp_c()} C above {MAX_TEMP_C} C"
                time.sleep(0.5)
            if _stop["flag"]:
                break

        time.sleep(3)  # let the last second's line land
        rows = read_metrics(metrics_path)

        # ---- postflight (before restart, so the env is still observable) --
        pid = service_pid()
        post = {
            "finished_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "temp_c": temp_c(),
            "throttled": throttled(),
            "disk_free_mb": disk_free_mb(),
            "service_active": sh(["systemctl", "is-active", "transitguard"])[1],
            "buffer_count": get_json(DATA_URL).get("buffer_count"),
            "metrics_lines": len(rows),
            "trace_var_present_before_teardown": process_has_env(pid, "CAMERA_METRICS_LOG_PATH"),
            "stop_reason": _stop["reason"],
        }
        write_json(os.path.join(run_dir, "postflight.json"), post)

    finally:
        remove_dropin()
        # Wait for the restarted service so the environment check is meaningful.
        deadline = time.time() + 60
        while time.time() < deadline:
            if sh(["systemctl", "is-active", "transitguard"])[1] == "active" and service_pid() > 0:
                break
            time.sleep(2)
        time.sleep(15)
        pid = service_pid()
        post2 = {
            "teardown_utc": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "dropin_exists": os.path.exists(DROPIN),
            "cam_metrics_var_in_process": process_has_env(pid, "CAMERA_METRICS_LOG_PATH"),
            "service_active": sh(["systemctl", "is-active", "transitguard"])[1],
            "temp_c": temp_c(),
        }
        write_json(os.path.join(run_dir, "postflight_teardown.json"), post2)
        teardown_done = True

    # ---- local summary ------------------------------------------------
    per_phase = {}
    for p in phases:
        per_phase[p["name"]] = summarize(rows, p["start_epoch"], p["end_epoch"])
    labels_ok = None
    if len(phases) == 3 and all(per_phase[p["name"]]["samples"] for p in phases):
        labels_ok = (per_phase["present_1"]["face_present_pct"] >= 50
                     and per_phase["absent_1"]["face_present_pct"] <= 10
                     and per_phase["present_2"]["face_present_pct"] >= 50)

    summary = {
        "run": stamp,
        "metrics_lines": len(rows),
        "phases": per_phase,
        "phase_labels_match_observed": labels_ok,
        "teardown_done": teardown_done,
        "video_retained": False,
        "totals": summarize(rows, 0, float("inf")),
    }
    write_json(os.path.join(run_dir, "summary.json"), summary)

    with open(os.path.join(run_dir, "samples.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(samples[0].keys()) if samples else ["wall_utc"])
        w.writeheader()
        w.writerows(samples)

    print("\n=== per phase")
    for name, s in per_phase.items():
        print(f"  {name}: samples={s.get('samples')} face={s.get('face_present_pct')}% "
              f"frontal={s.get('frontal_pct')}% ear_avail={s.get('ear_available_pct')}% "
              f"fps={s.get('fps_median')} states={s.get('driver_states')}")
    print(f"=== labels match observed: {labels_ok}")
    print(f"=== metrics lines: {len(rows)}; teardown done: {teardown_done}")
    print(f"=== artifacts: {run_dir}")


if __name__ == "__main__":
    main()
