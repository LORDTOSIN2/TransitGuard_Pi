#!/usr/bin/env python3
"""
Record MQ-3 alcohol metrics from the running TransitGuard dashboard.

This is a passive recorder: it polls the local `/data` endpoint once per
second and writes a CSV, so it does not touch the ADS1115 or the app. It is
intended for a simple functional/relative alcohol-vapor test, not a
calibrated breathalyzer measurement.

Usage (on the Pi):
    ./fatigue-env/bin/python3 tools/record_alcohol_test.py \
        --duration 900 --out logs/alcohol_test_<UTC>.csv

The operator can expose the sensor whenever ready; the CSV timestamps and the
ratio dip identify the response. Do not touch or wet the sensor.
"""
import argparse
import csv
import datetime as dt
import json
import os
import signal
import statistics as stats
import time
import urllib.request

FIELDS = [
    "timestamp_utc",
    "elapsed_s",
    "alcohol_ratio",
    "alcohol_ppm",
    "driver_state",
    "alert_threshold",
    "caution_threshold",
    "pi_temperature",
    "pi_throttled",
    "fetch_ok",
    "error",
]


def utc_now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch(url):
    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read().decode("utf-8"))


def num(v):
    return v if isinstance(v, (int, float)) else None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://127.0.0.1:8080/data")
    ap.add_argument("--duration", type=float, default=900.0,
                    help="recording duration in seconds (default 900)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="poll interval in seconds (default 1.0)")
    ap.add_argument("--out", default="",
                    help="CSV path (default logs/alcohol_test_<UTC>.csv)")
    args = ap.parse_args()

    def _stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or os.path.join("logs", f"alcohol_test_{stamp}.csv")
    parent = os.path.dirname(os.path.abspath(out))
    os.makedirs(parent, exist_ok=True)

    start_utc = utc_now()
    start_mono = time.monotonic()
    rows = []
    errors = 0

    print(f"recording {args.url} -> {out} for {args.duration:.0f}s")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        writer.writeheader()
        fh.flush()
        try:
            while True:
                elapsed = time.monotonic() - start_mono
                if elapsed >= args.duration:
                    break
                t0 = time.monotonic()
                try:
                    d = fetch(args.url)
                    ok = 1
                    err = ""
                except Exception as exc:  # keep recording through app hiccups
                    d = {}
                    ok = 0
                    err = str(exc)
                    errors += 1
                row = {
                    "timestamp_utc": utc_now(),
                    "elapsed_s": round(elapsed, 2),
                    "alcohol_ratio": num(d.get("alcohol_ratio")),
                    "alcohol_ppm": num(d.get("alcohol_ppm")),
                    "driver_state": d.get("driver_state", ""),
                    "alert_threshold": num(d.get("alcohol_alert_threshold")),
                    "caution_threshold": num(d.get("alcohol_caution_threshold")),
                    "pi_temperature": num(d.get("pi_temperature")),
                    "pi_throttled": d.get("pi_throttled", ""),
                    "fetch_ok": ok,
                    "error": err,
                }
                writer.writerow(row)
                fh.flush()
                rows.append(row)
                time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            print("interrupted")

    ratios = [r["alcohol_ratio"] for r in rows if r["alcohol_ratio"] is not None]
    alerts = [r for r in rows if r["driver_state"] == "ALCOHOL_ALERT"]
    summary = {
        "csv": os.path.abspath(out),
        "url": args.url,
        "started_utc": start_utc,
        "finished_utc": utc_now(),
        "duration_s": round(time.monotonic() - start_mono, 2),
        "samples": len(rows),
        "fetch_errors": errors,
        "ratio_n": len(ratios),
        "ratio_min": min(ratios) if ratios else None,
        "ratio_min_utc": next((r["timestamp_utc"] for r in rows
                               if r["alcohol_ratio"] == min(ratios)), None)
        if ratios else None,
        "ratio_max": max(ratios) if ratios else None,
        "ratio_mean": round(stats.mean(ratios), 4) if ratios else None,
        "ratio_last": ratios[-1] if ratios else None,
        "alert_samples": len(alerts),
        "first_alert_utc": alerts[0]["timestamp_utc"] if alerts else None,
        "last_alert_utc": alerts[-1]["timestamp_utc"] if alerts else None,
        "alert_threshold": next((r["alert_threshold"] for r in rows
                                 if r["alert_threshold"] is not None), None),
        "caution_threshold": next((r["caution_threshold"] for r in rows
                                   if r["caution_threshold"] is not None), None),
        "note": "relative MQ-3 ratio only; no BAC/BrAC or calibrated ppm claim",
    }
    summary_path = os.path.splitext(out)[0] + "_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, sort_keys=True)
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"wrote {out}")
    print(f"wrote {summary_path}")


if __name__ == "__main__":
    main()
