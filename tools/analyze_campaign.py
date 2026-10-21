#!/usr/bin/env python3
"""
TransitGuard post-mitigation campaign reconciler.

Reads one or more run folders produced by tools/run_session.py, reconciles the
Pi-side telemetry trace against the authoritative Supabase `public.telemetry`
rows, and writes per-run plus combined statistics.

    python tools/analyze_campaign.py logs/runs/20261021T*_s1 \
        --out docs/measurements/post_mitigation

Outputs
-------
    <run>/reconciled_messages.csv   one row per scheduled message
    <run>/summary.json              augmented with reconciliation results
    <out>/campaign_summary.json     machine-readable, per run + combined
    <out>/campaign_tables.md        Markdown tables for the report

Definitions (printed in the output so numbers cannot be misread)
---------------------------------------------------------------
  scheduled            messages queued by the app (trace `scheduled`)
  attempted            HTTP POSTs made (trace `attempt`); includes retries
  attempted_unique     distinct messageIds posted
  retried              attempted - attempted_unique
  dropped              evicted from the bounded in-memory queue (trace `dropped`)
  stored_unique        distinct ids actually present in Supabase for the window
  http_acceptance      attempts that returned HTTP 201 / attempts
  stored_per_attempt   stored_unique / attempted
  stored_per_scheduled stored_unique / scheduled

Duplicate caveat: the ingest function upserts with ignoreDuplicates, so a
duplicate returns 201 and stores nothing. Stored counts are therefore taken
from the table, never from HTTP status alone.
"""
import argparse
import collections
import csv
import datetime as dt
import json
import math
import os
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://hfzqfcuezuxcgaifxqok.supabase.co")
PUBLISHABLE_KEY = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF")
DEVICE_ID = os.environ.get("TRANSITGUARD_DEVICE_ID", "BUS-101")

SELECT_COLS = ",".join([
    "message_id", "device_id", "created_at", "timestamp",
    "transport_mode", "wifi_ssid", "wifi_rssi", "driver_state",
])


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def epoch(iso):
    if iso is None:
        return None
    s = iso.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, offset = rest, ""
        for marker in ("+", "-"):
            idx = rest.find(marker)
            if idx != -1:
                frac, offset = rest[:idx], rest[idx:]
                break
        s = f"{head}.{(frac + '000000')[:6]}{offset}"
    return dt.datetime.fromisoformat(s).timestamp()


def pctl(values, p):
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * p / 100.0
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vs[int(k)]
    return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)


def describe(values, nd=1):
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), nd),
        "median": round(statistics.median(values), nd),
        "mean": round(statistics.mean(values), nd),
        "p90": round(pctl(values, 90), nd),
        "p95": round(pctl(values, 95), nd),
        "p99": round(pctl(values, 99), nd),
        "stdev": round(statistics.stdev(values), nd) if len(values) > 1 else None,
        "max": round(max(values), nd),
    }


def fetch_rows(start_iso, end_iso):
    rows, offset, page = [], 0, 1000
    while True:
        q = urllib.parse.urlencode({
            "select": SELECT_COLS,
            "device_id": f"eq.{DEVICE_ID}",
            "created_at": f"gte.{start_iso}",
            "order": "created_at.asc",
            "limit": page,
            "offset": offset,
        })
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/telemetry?{q}",
            headers={"apikey": PUBLISHABLE_KEY,
                     "Authorization": f"Bearer {PUBLISHABLE_KEY}"})
        try:
            with urllib.request.urlopen(req, timeout=45) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"Supabase read failed: HTTP {exc.code} "
                             f"{exc.read()[:300]!r}")
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    end_epoch = epoch(end_iso)
    return [r for r in rows if epoch(r["created_at"]) <= end_epoch + 1]


def load_trace(path):
    events = []
    if not os.path.exists(path):
        return events
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def as_float(row, key):
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError):
        return None


def is_true(value):
    return str(value).strip().lower() == "true"


# --------------------------------------------------------------------------
# per-run analysis
# --------------------------------------------------------------------------
def analyze_run(run_dir):
    trace_path = os.path.join(run_dir, "telemetry_trace.jsonl")
    events = load_trace(trace_path)
    samples = load_csv(os.path.join(run_dir, "samples.csv"))
    flaps = load_csv(os.path.join(run_dir, "flaps.csv"))
    try:
        with open(os.path.join(run_dir, "summary.json"), encoding="utf-8") as fh:
            harness_summary = json.load(fh)
    except (OSError, ValueError):
        harness_summary = {}
    try:
        with open(os.path.join(run_dir, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError):
        manifest = {}

    if not events:
        raise SystemExit(f"{run_dir}: no trace events (tracing was not enabled?)")

    # The harness records the intended session window. Trust it over the trace
    # file bounds: a late restart would otherwise count post-session messages.
    win = harness_summary.get("window") or {}
    w_start, w_end = win.get("start_epoch"), win.get("end_epoch")
    if w_start and w_end:
        events = [e for e in events
                  if w_start <= (e.get("wall_epoch") or 0) <= w_end]
        if not events:
            raise SystemExit(f"{run_dir}: no trace events inside the recorded window")

    sched = [e for e in events if e.get("ev") == "scheduled"]
    att = [e for e in events if e.get("ev") == "attempt"]
    drops = [e for e in events if e.get("ev") == "dropped"]

    wall = [e["wall_epoch"] for e in events if "wall_epoch" in e]
    t_start, t_end = (w_start, w_end) if (w_start and w_end) else (min(wall), max(wall))
    start_iso = dt.datetime.fromtimestamp(t_start - 120, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
    end_iso = dt.datetime.fromtimestamp(t_end + 120, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
    rows = fetch_rows(start_iso, end_iso)
    rows_in_window = [r for r in rows
                      if t_start <= (epoch(r["created_at"]) or 0) <= t_end]
    by_id = {r["message_id"]: r for r in rows}
    stored_ids = set(by_id)

    attempted_ids = [a.get("messageId") for a in att if a.get("messageId")]
    distinct_attempted = set(attempted_ids)
    sched_ids = [s.get("messageId") for s in sched if s.get("messageId")]
    distinct_scheduled = set(sched_ids)
    ok_201 = [a for a in att if a.get("http_status") == 201]
    stored_matched = stored_ids & distinct_attempted
    unmatched_201 = {a.get("messageId") for a in ok_201} - stored_ids
    retries = len(attempted_ids) - len(distinct_attempted)
    never_attempted = distinct_scheduled - distinct_attempted

    rtts = [a["rtt_ms"] for a in att if a.get("rtt_ms") is not None]
    ok_rtts = [a["rtt_ms"] for a in ok_201 if a.get("rtt_ms") is not None]
    fail_rtts = [a["rtt_ms"] for a in att
                 if a.get("http_status") != 201 and a.get("rtt_ms") is not None]

    sched_ts = sorted(s["wall_epoch"] for s in sched if "wall_epoch" in s)
    gaps = [b - a for a, b in zip(sched_ts, sched_ts[1:]) if 0 < b - a < 120]

    # per-message CSV
    status_by_id = {}
    for a in att:
        mid = a.get("messageId")
        if mid:
            status_by_id.setdefault(mid, []).append(a)
    rec_path = os.path.join(run_dir, "reconciled_messages.csv")
    with open(rec_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["message_id", "scheduled_utc", "attempts", "last_http_status",
                    "last_rtt_ms", "stored", "created_at", "driver_state",
                    "trigger", "test_injected_state", "stored_after_s"])
        sched_by_id = {s.get("messageId"): s for s in sched}
        for mid in sorted(set(sched_ids) | distinct_attempted,
                          key=lambda m: (sched_by_id.get(m) or {}).get("wall_epoch", 0)):
            s = sched_by_id.get(mid) or {}
            attempts = status_by_id.get(mid, [])
            last = attempts[-1] if attempts else {}
            row = by_id.get(mid)
            created = epoch(row["created_at"]) if row else None
            w.writerow([
                mid,
                (dt.datetime.fromtimestamp(s["wall_epoch"], dt.timezone.utc)
                 .strftime("%Y-%m-%dT%H:%M:%SZ") if s.get("wall_epoch") else ""),
                len(attempts),
                last.get("http_status"),
                last.get("rtt_ms"),
                bool(row),
                row["created_at"] if row else "",
                s.get("driver_state", ""),
                s.get("trigger", ""),
                s.get("test_injected_state", ""),
                round(created - s["wall_epoch"], 2)
                if created and s.get("wall_epoch") else "",
            ])

    # sample-derived availability and queue high-water
    temps = [v for v in (as_float(r, "temp_c") for r in samples) if v is not None]
    queue_vals = [v for v in (as_float(r, "telemetry_queue_size") for r in samples)
                  if v is not None]
    buf_vals = [v for v in (as_float(r, "buffer_count") for r in samples)
                if v is not None]
    trace_queue = [s.get("queue_size") or 0 for s in sched]
    fix_flags = [is_true(r.get("fix_valid")) for r in samples] if samples else []
    first_fix = None
    for r in samples:
        if is_true(r.get("fix_valid")):
            first_fix = as_float(r, "elapsed_s")
            break
    camera_any = [any(is_true(r.get(k)) for k in
                      ("ear_present", "mar_present", "perclos_present", "pitch_present"))
                  for r in samples] if samples else []
    heartbeat_ok = [bool(r.get("heartbeat")) for r in samples] if samples else []

    n_att = len(att)
    acc_unique = len(stored_matched)
    result = {
        "run_id": os.path.basename(run_dir.rstrip("/\\")),
        "session": manifest.get("session"),
        "window_start_utc": dt.datetime.fromtimestamp(
            t_start, dt.timezone.utc).isoformat(),
        "window_end_utc": dt.datetime.fromtimestamp(
            t_end, dt.timezone.utc).isoformat(),
        "duration_min": round((t_end - t_start) / 60.0, 2),
        "window_source": win.get("source", "trace-bounds" if not w_start else "harness"),
        "hotspot": (manifest.get("network_at_start") or {}).get("ssid"),
        "rssi_dbm": (manifest.get("network_at_start") or {}).get("rssi_dbm"),
        "aborted": harness_summary.get("aborted"),
        "abort_reason": harness_summary.get("abort_reason"),
        "scheduled": len(sched),
        "scheduled_unique": len(distinct_scheduled),
        "attempted": n_att,
        "attempted_unique": len(distinct_attempted),
        "accepted_http201": len(ok_201),
        "stored_unique": acc_unique,
        "rows_in_window": len(rows_in_window),
        "rows_fetched_padded": len(rows),
        "dropped": len(drops),
        "retried": retries,
        "scheduled_never_attempted": len(never_attempted),
        "http201_but_not_stored": len(unmatched_201),
        "http_acceptance_pct": round(100.0 * len(ok_201) / n_att, 2) if n_att else None,
        "stored_per_attempt_pct": round(100.0 * acc_unique / n_att, 2) if n_att else None,
        "stored_per_scheduled_pct": (round(100.0 * acc_unique / len(distinct_scheduled), 2)
                                     if distinct_scheduled else None),
        "queue_high_water": max([int(max(trace_queue)) if trace_queue else 0,
                                 int(max(queue_vals)) if queue_vals else 0]),
        "buffer_high_water": int(max(buf_vals)) if buf_vals else None,
        "cadence_s": {
            "median": round(statistics.median(gaps), 2) if gaps else None,
            "mean": round(statistics.mean(gaps), 2) if gaps else None,
            "min": round(min(gaps), 2) if gaps else None,
            "max": round(max(gaps), 2) if gaps else None,
        },
        "rtt_ms_all_attempts": describe(rtts),
        "rtt_ms_http201": describe(ok_rtts),
        "rtt_ms_failures": describe(fail_rtts),
        "safety": {
            "pre": (manifest.get("preflight") or {}).get("temp_c"),
            "post": ((harness_summary.get("safety") or {}).get("post") or {}).get("temp_c"),
            "temp_max_sampled": max(temps) if temps else None,
            "throttled_pre": (manifest.get("preflight") or {}).get("throttled_hex"),
            "throttled_post": (((harness_summary.get("safety") or {}).get("post") or {})
                               .get("throttled_hex")),
        },
        "availability": {
            "sample_count": len(samples),
            "heartbeat_present_pct": (round(100.0 * sum(heartbeat_ok) / len(samples), 1)
                                      if samples else None),
            "gps_fix_pct": (round(100.0 * sum(fix_flags) / len(samples), 1)
                            if samples else None),
            "gps_first_fix_elapsed_s": first_fix,
            "camera_metric_present_pct": (round(100.0 * sum(camera_any) / len(samples), 1)
                                          if samples else None),
        },
        "scripted_flaps": len(flaps),
        # kept out of JSON (lists would bloat it); used for pooled statistics
        "_rtt_ms_http201": ok_rtts,
        "manifest": {
            "app_git_head": (manifest.get("software") or {}).get("app_git_head"),
            "app_file_sha256": (manifest.get("software") or {}).get("app_file_sha256"),
            "model_sha256": (manifest.get("software") or {}).get("model_sha256"),
            "python": (manifest.get("software") or {}).get("python"),
            "kernel": (manifest.get("software") or {}).get("kernel"),
        },
    }

    # merge into the harness summary so both live in one place
    harness_summary["reconciliation"] = result
    with open(os.path.join(run_dir, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(harness_summary, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return result


# --------------------------------------------------------------------------
# combined + reporting
# --------------------------------------------------------------------------
def combined(runs):
    def total(key):
        return sum(r[key] or 0 for r in runs)

    sched = total("scheduled")
    stored = total("stored_unique")
    attempted = total("attempted")
    ok201 = total("accepted_http201")
    # RTT values are not additive; recompute from the per-run descriptions is
    # impossible, so report the pooled *set* only if callers supply it.
    return {
        "runs": len(runs),
        "scheduled": sched,
        "attempted": attempted,
        "attempted_unique": total("attempted_unique"),
        "accepted_http201": ok201,
        "stored_unique": stored,
        "dropped": total("dropped"),
        "retried": total("retried"),
        "scheduled_never_attempted": total("scheduled_never_attempted"),
        "http_acceptance_pct": round(100.0 * ok201 / attempted, 2) if attempted else None,
        "stored_per_attempt_pct": (round(100.0 * stored / attempted, 2)
                                   if attempted else None),
        "stored_per_scheduled_pct": round(100.0 * stored / sched, 2) if sched else None,
        "queue_high_water_max": max((r["queue_high_water"] or 0) for r in runs),
        "duration_min_total": round(sum(r["duration_min"] for r in runs), 1),
    }


def md_table(runs, comb):
    lines = []
    lines.append("| Metric | " + " | ".join(
        f"Run {r['session'] or i + 1}" for i, r in enumerate(runs)) + " | Combined |")
    lines.append("|---|" + "---|" * (len(runs) + 1))

    def row(label, key, fmt="{:.0f}", comb_key=None, comb_fmt=None):
        vals = []
        for r in runs:
            v = r.get(key)
            vals.append(fmt.format(v) if isinstance(v, (int, float)) else "—")
        cv = comb.get(comb_key or key)
        vals.append((comb_fmt or fmt).format(cv) if isinstance(cv, (int, float)) else "—")
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    row("Scheduled", "scheduled")
    row("Attempted (HTTP POSTs)", "attempted")
    row("Attempted unique", "attempted_unique")
    row("Stored unique (server)", "stored_unique")
    row("Dropped from queue", "dropped")
    row("Retried", "retried")
    row("Scheduled, never attempted", "scheduled_never_attempted")
    row("HTTP 201 acceptance, %", "http_acceptance_pct", "{:.2f}")
    row("Stored / attempted, %", "stored_per_attempt_pct", "{:.2f}")
    row("Stored / scheduled, %", "stored_per_scheduled_pct", "{:.2f}")
    row("Queue high-water (msgs)", "queue_high_water", "{:.0f}", "queue_high_water_max")
    row("Duration, min", "duration_min", "{:.1f}", "duration_min_total")

    lines.append("")
    lines.append("| RTT (ms) | " + " | ".join(
        f"Run {r['session'] or i + 1}" for i, r in enumerate(runs)) + " | Combined |")
    lines.append("|---|" + "---|" * (len(runs) + 1))
    for stat in ("n", "min", "median", "mean", "p90", "p95", "p99", "stdev", "max"):
        vals = []
        for r in runs:
            v = r["rtt_ms_http201"].get(stat)
            vals.append(f"{v:g}" if isinstance(v, (int, float)) else "—")
        cv = (comb.get("rtt_ms_http201") or {}).get(stat)
        vals.append(f"{cv:g}" if isinstance(cv, (int, float)) else "—")
        lines.append(f"| {stat} | " + " | ".join(vals) + " |")

    lines.append("")
    lines.append("| Availability | " + " | ".join(
        f"Run {r['session'] or i + 1}" for i, r in enumerate(runs)) + " |")
    lines.append("|---|" + "---|" * len(runs))
    for label, key, fmt in (
        ("Heartbeat present, %", "heartbeat_present_pct", "{:.1f}"),
        ("GPS fix available, %", "gps_fix_pct", "{:.1f}"),
        ("Camera metric present, %", "camera_metric_present_pct", "{:.1f}"),
    ):
        vals = [fmt.format(r["availability"][key]) if r["availability"].get(key) is not None
                else "—" for r in runs]
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    lines.append("")
    lines.append("| Safety | " + " | ".join(
        f"Run {r['session'] or i + 1}" for i, r in enumerate(runs)) + " |")
    lines.append("|---|" + "---|" * len(runs))
    for label, key in (("SoC before, C", "pre"), ("SoC after, C", "post"),
                       ("SoC max sampled, C", "temp_max_sampled")):
        vals = []
        for r in runs:
            v = r["safety"].get(key)
            vals.append(f"{v:g}" if isinstance(v, (int, float)) else "—")
        lines.append(f"| {label} | " + " | ".join(vals) + " |")
    vals = [(r["safety"].get("throttled_pre") or "—") for r in runs]
    lines.append("| get_throttled before | " + " | ".join(vals) + " |")
    vals = [(r["safety"].get("throttled_post") or "—") for r in runs]
    lines.append("| get_throttled after | " + " | ".join(vals) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+", help="run folders from run_session.py")
    ap.add_argument("--out", default="docs/measurements/post_mitigation")
    args = ap.parse_args()

    results = []
    for run_dir in args.runs:
        print(f"analysing {run_dir} ...")
        results.append(analyze_run(run_dir))
    results.sort(key=lambda r: (r.get("session") or 0, r["window_start_utc"]))
    pooled_rtt = []
    for r in results:
        pooled_rtt.extend(r.pop("_rtt_ms_http201", []) or [])
    comb = combined(results)
    comb["rtt_ms_http201"] = describe(pooled_rtt)

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "campaign_summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"runs": results, "combined": comb,
                   "notes": [
                       "Per-session numbers are reported separately; the combined row "
                       "does not claim fleet or long-term reliability.",
                       "RTT is request->response on the Pi monotonic timer, not end-to-end.",
                       "Duplicate posts return HTTP 201 and store nothing; stored counts "
                       "come from the telemetry table.",
                   ]}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    table = md_table(results, comb)
    with open(os.path.join(args.out, "campaign_tables.md"), "w", encoding="utf-8") as fh:
        fh.write("# Post-mitigation campaign — generated tables\n\n")
        fh.write(f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC. "
                 f"Device `{DEVICE_ID}`.\n\n")
        fh.write(table + "\n")
    print()
    print(table)
    print(f"\nwrote {args.out}/campaign_summary.json and campaign_tables.md")


if __name__ == "__main__":
    sys.exit(main())
