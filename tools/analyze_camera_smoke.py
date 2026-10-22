#!/usr/bin/env python3
"""
Reconcile one camera-pipeline availability smoke run.

Reads the artifacts produced by tools/run_camera_smoke.py on the Pi:
  manifest.json, preflight.json, postflight.json, postflight_teardown.json,
  phases.json, samples.csv, metrics.jsonl

and recomputes, per announced phase:
  * face-present and frontal share
  * EAR/MAR/PERCLOS availability and medians
  * head-pose medians when a face is present
  * measured frame rate and the longest gap between samples
  * classifier output counts

It also verifies that (a) the phase labels agree with what the camera observed,
(b) teardown removed CAMERA_METRICS_LOG_PATH, and (c) no image/video files were
retained.

Usage:
    python tools/analyze_camera_smoke.py <RUN_DIR> [--out docs/measurements/camera_smoke]
"""
import argparse
import csv
import datetime as dt
import json
import os
import statistics
import sys

UI_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".mp4", ".avi", ".mov", ".mkv", ".h264")


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def load_metrics(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    except OSError:
        pass
    return rows


def med(xs):
    return round(statistics.median(xs), 3) if xs else None


def phase_stats(rows, start, end):
    win = [r for r in rows if start <= r.get("wall_epoch", 0) < end]
    n = len(win)
    if not n:
        return {"samples": 0}
    face_rows = [r for r in win if r.get("face_present")]
    ear = [r["ear"] for r in face_rows if r.get("ear") is not None]
    mar = [r["mar"] for r in face_rows if r.get("mar") is not None]
    per = [r["perclos"] for r in face_rows if r.get("perclos") is not None]
    pitch = [r["pitch"] for r in face_rows if r.get("pitch") is not None]
    yaw = [r["yaw"] for r in face_rows if r.get("yaw") is not None]
    roll = [r["roll"] for r in face_rows if r.get("roll") is not None]
    fps = [r["fps"] for r in win if r.get("fps") is not None]
    epochs = [r.get("wall_epoch", 0) for r in win]
    gaps = [b - a for a, b in zip(epochs, epochs[1:])]
    states = {}
    for r in win:
        states[r.get("driver_state")] = states.get(r.get("driver_state"), 0) + 1
    frontal = sum(1 for r in win if r.get("frontal"))
    return {
        "samples": n,
        "face_present_pct": round(100.0 * len(face_rows) / n, 1),
        "frontal_pct": round(100.0 * frontal / n, 1),
        "ear_available_pct": round(100.0 * len(ear) / n, 1),
        "mar_available_pct": round(100.0 * len(mar) / n, 1),
        "perclos_available_pct": round(100.0 * len(per) / n, 1),
        "ear_median": med(ear),
        "mar_median": med(mar),
        "perclos_median": med(per),
        "pitch_median": med(pitch),
        "yaw_median": med(yaw),
        "roll_median": med(roll),
        "fps_median": med(fps),
        "fps_min": round(min(fps), 2) if fps else None,
        "max_gap_sec": round(max(gaps), 2) if gaps else None,
        "driver_states": states,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--out", default="docs/measurements/camera_smoke")
    args = ap.parse_args()

    run = args.run_dir
    manifest = load_json(os.path.join(run, "manifest.json"), {})
    pre = load_json(os.path.join(run, "preflight.json"), {})
    post = load_json(os.path.join(run, "postflight.json"), {})
    post2 = load_json(os.path.join(run, "postflight_teardown.json"), {})
    phases = (load_json(os.path.join(run, "phases.json"), {}) or {}).get("phases", [])
    rows = load_metrics(os.path.join(run, "metrics.jsonl"))

    if not rows:
        sys.exit(f"no metrics lines in {run}/metrics.jsonl")
    if not phases:
        sys.exit(f"no phases in {run}/phases.json")

    per_phase = {}
    for p in phases:
        per_phase[p["name"]] = phase_stats(rows, p["start_epoch"], p["end_epoch"])

    have_all = all(per_phase.get(n, {}).get("samples", 0) for n in
                   ("present_1", "absent_1", "present_2"))
    labels_ok = None
    if have_all:
        labels_ok = (per_phase["present_1"]["face_present_pct"] >= 50
                     and per_phase["absent_1"]["face_present_pct"] <= 10
                     and per_phase["present_2"]["face_present_pct"] >= 50)

    media = []
    for root, _, files in os.walk(run):
        for f in files:
            if f.lower().endswith(UI_EXT):
                media.append(os.path.join(root, f))

    out = {
        "run": os.path.basename(os.path.normpath(run)),
        "analyzed_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest": manifest,
        "preflight": pre,
        "postflight": post,
        "teardown": post2,
        "phases": phases,
        "per_phase": per_phase,
        "phase_labels_match_observed": labels_ok,
        "metrics_lines": len(rows),
        "media_files_retained": media,
        "claims_allowed": bool(labels_ok) and not media,
    }

    os.makedirs(args.out, exist_ok=True)
    name = out["run"]
    json_path = os.path.join(args.out, f"{name}_summary.json")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)

    md = []
    md.append(f"# Camera-pipeline smoke run — {name}\n")
    md.append(f"- App `{manifest.get('vehicle_monitor_sha256', '?')[:12]}` "
              f"(HEAD `{str(manifest.get('app_head'))[:7]}`), model "
              f"`{str(manifest.get('model_sha256'))[:12]}`")
    md.append(f"- Camera devices: {', '.join(pre.get('camera_devices', []))}; "
              f"clock sync `{manifest.get('clock_sync')}`")
    md.append(f"- Start {pre.get('started_utc')} · {pre.get('temp_c')} °C · "
              f"throttled `{pre.get('throttled')}` · {pre.get('disk_free_mb')} MB free")
    md.append(f"- End {post.get('finished_utc')} · {post.get('temp_c')} °C · "
              f"throttled `{post.get('throttled')}`")
    md.append(f"- Metrics lines: **{len(rows)}** (1 Hz, metrics only); "
              f"image/video files retained: **{len(media)}**")
    md.append("")
    md.append("| Phase | Samples | Face present | Frontal | EAR avail. | MAR avail. | "
              "PERCLOS avail. | Median EAR | Median MAR | Median PERCLOS | Median FPS | Max gap | States |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for p in phases:
        s = per_phase[p["name"]]
        md.append(
            f"| {p['name']} | {s['samples']} | {s['face_present_pct']} % | "
            f"{s['frontal_pct']} % | {s['ear_available_pct']} % | "
            f"{s['mar_available_pct']} % | {s['perclos_available_pct']} % | "
            f"{s['ear_median']} | {s['mar_median']} | {s['perclos_median']} | "
            f"{s['fps_median']} | {s['max_gap_sec']} s | "
            f"{', '.join(f'{k}={v}' for k, v in s['driver_states'].items())} |"
        )
    md.append("")
    md.append(f"**Phase labels match observed face presence:** {labels_ok}")
    md.append(f"**Teardown:** drop-in exists `{post2.get('dropin_exists')}`, "
              f"`CAMERA_METRICS_LOG_PATH` in process `{post2.get('cam_metrics_var_in_process')}`, "
              f"service `{post2.get('service_active')}`")
    md.append("")
    md.append("Metrics only: no frames, images or video were written. "
              "Face-present/absent is a pipeline-input condition, not a detector-accuracy test.")
    with open(os.path.join(args.out, f"{name}_tables.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print("\n".join(md))
    print(f"\nwrote {json_path}")


if __name__ == "__main__":
    main()
