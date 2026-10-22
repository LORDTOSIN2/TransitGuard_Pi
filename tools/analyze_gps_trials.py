#!/usr/bin/env python3
"""
Reconcile one GPS open-sky availability run produced by tools/run_gps_trials.py.

Reads manifest.json, run_summary.json and each trial_*/{samples.csv,summary.json}
and computes, per trial and combined:

  * TTFF (reset -> first valid fix)
  * fix-valid fraction over the observation window
  * satellites used / in view, best SNR, HDOP
  * age of the last valid fix (max and at end)
  * fix losses after acquisition
  * whether the receiver-level cold start was observed (NMEA gap >= 2 s)

Availability only: no coordinates are stored, so no position-accuracy claim is
possible or made.

Usage:
    python tools/analyze_gps_trials.py <RUN_DIR> [--out docs/measurements/gps_open_sky]
"""
import argparse
import csv
import datetime as dt
import json
import os
import statistics as stats
import sys

COORD_COLUMNS = ("lat", "latitude", "lon", "longitude", "position")


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def load_samples(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            return list(csv.DictReader(fh))
    except OSError:
        return []


def med(xs):
    return round(stats.median(xs), 2) if xs else None


def mean(xs):
    return round(sum(xs) / len(xs), 2) if xs else None


def as_int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--out", default="docs/measurements/gps_open_sky")
    args = ap.parse_args()

    run = args.run_dir
    manifest = load_json(os.path.join(run, "manifest.json"), {})
    run_summary = load_json(os.path.join(run, "run_summary.json"), {})
    trial_dirs = sorted(d for d in os.listdir(run)
                        if d.startswith("trial_") and os.path.isdir(os.path.join(run, d)))
    if not trial_dirs:
        sys.exit(f"no trial_* folders in {run}")

    trials = []
    for d in trial_dirs:
        summary = load_json(os.path.join(run, d, "summary.json"), {})
        samples = load_samples(os.path.join(run, d, "samples.csv"))
        if not samples:
            continue
        valid = [r for r in samples if r.get("fix_valid") == "True"]
        sats = [as_int(r["sats_used"]) for r in valid if as_int(r["sats_used"]) is not None]
        view = [as_int(r["sats_in_view"]) for r in samples
                if as_int(r["sats_in_view"]) is not None]
        snr = [as_float(r["best_snr"]) for r in samples
               if as_float(r["best_snr"]) is not None]
        hdop = [as_float(r["hdop"]) for r in valid if as_float(r["hdop"]) is not None]
        stale = [as_float(r["sec_since_last_fix"]) for r in samples
                 if as_float(r["sec_since_last_fix"]) is not None]
        elapsed = [as_float(r["elapsed_s"]) for r in samples if as_float(r["elapsed_s"]) is not None]
        first_fix = None
        if valid and elapsed:
            first_fix = next((e for r, e in zip(samples, elapsed)
                              if r.get("fix_valid") == "True"), None)
        t = {
            "folder": d,
            "trial": summary.get("trial"),
            "boot_id": summary.get("boot_id"),
            "pi_uptime_at_start_s": summary.get("pi_uptime_at_start_s"),
            "complete": summary.get("complete"),
            "completed_reason": summary.get("completed_reason"),
            "samples": len(samples),
            "valid_samples": len(valid),
            "fix_valid_fraction": round(len(valid) / len(samples), 3),
            "post_fix_samples": summary.get("post_fix_samples"),
            "post_fix_fix_valid_fraction": summary.get("post_fix_fix_valid_fraction"),
            "ttff_s": summary.get("ttff_s", first_fix),
            "ttff_from_samples_s": first_fix,
            "sats_used_median": med(sats),
            "sats_used_min": min(sats) if sats else None,
            "sats_used_max": max(sats) if sats else None,
            "sats_in_view_median": med(view),
            "sats_in_view_max": max(view) if view else None,
            "best_snr_median": med(snr),
            "best_snr_max": max(snr) if snr else None,
            "hdop_median": med(hdop),
            "stale_age_max_s": max(stale) if stale else None,
            "stale_age_end_s": stale[-1] if stale else None,
            "fix_losses": summary.get("fix_losses", 0),
            "nmea_gap_after_reset_s": summary.get("nmea_gap_after_reset_s"),
            "cold_start_observed": summary.get("cold_start_observed"),
            "cold_start_basis": summary.get("cold_start_basis"),
            "nmea_lines": summary.get("nmea_lines"),
            "aborted": summary.get("aborted"),
            "temp_c_max": summary.get("temp_c_max"),
            "started_system_utc": summary.get("started_system_utc"),
        }
        trials.append(t)

    n = len(trials)
    if not trials:
        sys.exit(f"no usable trial samples in {run}")
    fixed = [t for t in trials if t["ttff_s"] is not None]
    ttffs = [t["ttff_s"] for t in fixed]
    boot_ids = [t["boot_id"] for t in trials if t["boot_id"]]
    distinct_boot_ids = len(set(boot_ids))
    total_samples = sum(t["samples"] for t in trials)
    total_valid = sum(t["valid_samples"] for t in trials)
    post_num = sum(t["post_fix_samples"] * t["post_fix_fix_valid_fraction"]
                   for t in trials
                   if t["post_fix_samples"] and t["post_fix_fix_valid_fraction"] is not None)
    post_den = sum(t["post_fix_samples"] for t in trials
                   if t["post_fix_samples"] and t["post_fix_fix_valid_fraction"] is not None)
    combined = {
        "trials": n,
        "trials_with_fix": len(fixed),
        "trials_holding_fix": sum(1 for t in trials
                                  if t["completed_reason"] == "fix_hold_complete"),
        "ttff_min_s": min(ttffs) if ttffs else None,
        "ttff_median_s": med(ttffs),
        "ttff_max_s": max(ttffs) if ttffs else None,
        "fix_valid_fraction_weighted": round(total_valid / total_samples, 3) if total_samples else None,
        "post_fix_fix_valid_fraction_weighted": (round(post_num / post_den, 3)
                                                 if post_den else None),
        "samples": total_samples,
        "valid_samples": total_valid,
        "distinct_boot_ids": distinct_boot_ids,
        "power_cycle_trials": bool(n > 1 and len(boot_ids) == n and distinct_boot_ids == n),
        "cold_start_observed_all": all(t["cold_start_observed"] for t in trials),
        "cold_start_bases": sorted({t["cold_start_basis"] for t in trials if t["cold_start_basis"]}),
        "complete_all": all(t["complete"] for t in trials),
        "aborted_any": any(t["aborted"] for t in trials),
        "coords_retained": False,
    }

    checks = {
        "coords_retained": False,
        "coord_columns_found": [],
        "query": ("successful fix acquisition and availability; no position-"
                  "accuracy or TTFF-under-motion claim"),
    }
    for t in trials:
        path = os.path.join(run, t["folder"], "samples.csv")
        try:
            with open(path, "r", encoding="utf-8", newline="") as fh:
                cols = [c.strip().lower() for c in (csv.DictReader(fh).fieldnames or [])]
        except OSError:
            cols = []
        for c in cols:
            if any(c.startswith(p) for p in COORD_COLUMNS):
                checks["coord_columns_found"].append(f"{t['folder']}:{c}")

    out = {
        "run": os.path.basename(os.path.normpath(run)),
        "analyzed_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest": manifest,
        "run_summary": run_summary,
        "trials": trials,
        "combined": combined,
        "checks": checks,
    }

    os.makedirs(args.out, exist_ok=True)
    name = out["run"]
    with open(os.path.join(args.out, f"{name}_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)

    module = manifest.get("module") or {}
    md = []
    md.append(f"# GPS open-sky availability — {name}\n")
    md.append(f"- Receiver: **{module.get('sw') or 'unknown'}** "
              f"(hardware `{module.get('hw') or '?'}`, UBX `{module.get('ubx')}`)")
    md.append(f"- Port `{manifest.get('port')}` @ {manifest.get('baud')} baud; "
              f"cold start: {manifest.get('cold_start_method')}")
    md.append(f"- Window: max {manifest.get('max_sec') or manifest.get('trial_sec')} s per trial, "
              f"post-fix hold {manifest.get('post_fix_sec')} s, "
              f"trials planned {manifest.get('target_trials') or manifest.get('trials_planned')}")
    md.append(f"- App `{str(manifest.get('vehicle_monitor_sha256'))[:12]}` "
              f"(HEAD `{str(manifest.get('app_head'))[:7]}`); clock sync "
              f"`{manifest.get('clock_sync')}`")
    md.append(f"- Start {manifest.get('started_utc') or manifest.get('armed_utc')} · "
              f"{manifest.get('temp_c_start') or manifest.get('temp_c_at_arm')} °C · "
              f"throttled `{manifest.get('throttled_start') or manifest.get('throttled_at_arm')}` · "
              f"{manifest.get('disk_free_mb')} MB free")
    md.append("")
    md.append("| Trial | Samples | Fix-valid | Post-fix valid | TTFF | Sats used med (min–max) | "
              "Sats in view med (max) | Best SNR med | HDOP med | Max stale age | "
              "Fix losses | NMEA gap after reset | Cold start observed | Basis | Uptime | Reason | Boot ID |")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for t in trials:
        pf = t.get("post_fix_fix_valid_fraction")
        pf_s = f"{pf * 100:.1f} %" if pf is not None else "n/a"
        md.append(
            f"| {t['trial']} | {t['samples']} | {t['fix_valid_fraction'] * 100:.1f} % | "
            f"{pf_s} | "
            f"{t['ttff_s']} s | {t['sats_used_median']} "
            f"({t['sats_used_min']}–{t['sats_used_max']}) | "
            f"{t['sats_in_view_median']} ({t['sats_in_view_max']}) | "
            f"{t['best_snr_median']} | {t['hdop_median']} | "
            f"{t['stale_age_max_s']} s | {t['fix_losses']} | "
            f"{t['nmea_gap_after_reset_s']} s | {t['cold_start_observed']} | "
            f"{t['cold_start_basis']} | {t['pi_uptime_at_start_s']} s | "
            f"{t['completed_reason']} | {str(t['boot_id'])[:8]} |"
        )
    md.append("")
    md.append("| Combined | Value |")
    md.append("|---|---|")
    md.append(f"| Trials with a fix | **{combined['trials_with_fix']} / {combined['trials']}** |")
    md.append(f"| Trials holding fix for the post-fix window | "
              f"**{combined['trials_holding_fix']} / {combined['trials']}** |")
    md.append(f"| TTFF min / median / max | {combined['ttff_min_s']} / "
              f"**{combined['ttff_median_s']}** / {combined['ttff_max_s']} s |")
    md.append(f"| Fix-valid fraction (all samples, includes cold start) | "
              f"**{combined['fix_valid_fraction_weighted'] * 100:.1f} %** "
              f"({combined['valid_samples']} / {combined['samples']}) |")
    post = combined["post_fix_fix_valid_fraction_weighted"]
    md.append(f"| Fix-valid fraction after first fix | "
              f"**{post * 100:.1f} %** |" if post is not None else
              "| Fix-valid fraction after first fix | n/a |")
    md.append(f"| Cold start observed in all trials | {combined['cold_start_observed_all']} |")
    md.append(f"| Cold-start basis | {', '.join(combined['cold_start_bases']) or 'n/a'} |")
    md.append(f"| Distinct boot IDs / power-cycle trials | {combined['distinct_boot_ids']} / "
              f"{combined['power_cycle_trials']} |")
    md.append(f"| Any trial aborted | {combined['aborted_any']} |")
    md.append(f"| Coordinates stored | {checks['coords_retained']} |")
    md.append("")
    if checks["coord_columns_found"]:
        md.append(f"**Coordinate columns found (should be empty):** "
                  f"{', '.join(checks['coord_columns_found'])}")
    md.append("Availability only: no surveyed reference, so no position-accuracy claim. "
              "TTFF is measured from the UBX cold-start command to the first valid GGA fix.")
    with open(os.path.join(args.out, f"{name}_tables.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(md) + "\n")

    print("\n".join(md))
    print(f"\nwrote {os.path.join(args.out, name + '_summary.json')}")


if __name__ == "__main__":
    main()
