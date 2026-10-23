#!/usr/bin/env python3
"""
Reconcile an integrated-test telemetry trace with Supabase.

The app's telemetry trace (enabled temporarily by run_integrated_test.py)
contains one `scheduled` event and one or more `attempt` events per message,
including the `messageId`, HTTP status and local attempt time.  Supabase's
`public.telemetry.message_id` is unique, so a message ID is an unambiguous
delivery key.

Run on the Pi (or after pulling the run folder):

    python3 tools/analyze_integrated_test.py \
        --run-dir logs/integrated/run_20261023T... \
        --supabase-url https://<project-ref>.supabase.co \
        --supabase-key <publishable/anon key>

The key can instead be supplied as SUPABASE_ANON_KEY (or SUPABASE_KEY) and the
URL as SUPABASE_URL.  The public anon key can read the telemetry table, but do
not put a service-role key in this script or in shell history.

What is reported
----------------
* scheduled / attempted / HTTP-201 / failed / dropped counts from the local
  trace;
* which scheduled message IDs are present in Supabase, which are missing, and
  the per-session delivery ratio;
* approximate end-to-end latency from the local attempt timestamp to the
  Supabase row's `created_at` (clock sync caveat);
* explicit caveats: HTTP 201 is Edge Function acceptance, not proof of a row;
  a message may be buffered and delivered after the query grace window.

The script does not modify the run's samples.  It writes
`delivery_supabase.json` next to the trace and updates the `delivery_supabase`
member of `summary.json` if present.
"""
import argparse
import datetime as dt
import json
import os
import statistics as stats
import sys
import urllib.parse
import urllib.request


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def read_trace(path):
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


def parse_iso(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def median(xs):
    return round(stats.median(xs), 1) if xs else None


def supabase_rows(base_url, key, device_id, start_epoch, end_epoch):
    base = base_url.rstrip("/")
    if base.endswith("/rest/v1"):
        endpoint = base + "/telemetry"
    else:
        endpoint = base + "/rest/v1/telemetry"
    start = dt.datetime.fromtimestamp(start_epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = dt.datetime.fromtimestamp(end_epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {
        "select": "message_id,created_at,timestamp,driver_state,device_id",
        "device_id": f"eq.{device_id}",
        "created_at": f"gte.{start}",
        "order": "created_at.asc",
        "limit": "1000",
    }
    # PostgREST needs a second range filter; keep it as a separate query key.
    url = endpoint + "?" + urllib.parse.urlencode(params) + "&created_at=lte." + urllib.parse.quote(end)
    req = urllib.request.Request(url, headers={
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8")), url


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--supabase-url", default=os.environ.get("SUPABASE_URL", ""))
    ap.add_argument("--supabase-key",
                    default=os.environ.get("SUPABASE_ANON_KEY")
                    or os.environ.get("SUPABASE_KEY", ""))
    ap.add_argument("--device-id", default="")
    ap.add_argument("--grace-sec", type=int, default=300,
                    help="query this many seconds after the last session (default 300)")
    args = ap.parse_args()

    run_dir = os.path.abspath(args.run_dir)
    manifest = read_json(os.path.join(run_dir, "manifest.json"), {}) or {}
    phases = (read_json(os.path.join(run_dir, "phases.json"), {}) or {}).get("phases", [])
    trace_path = manifest.get("trace_path") or ""
    # A run folder pulled to another machine still has the trace beside the
    # manifest; the manifest's absolute Pi path is not portable.
    if not trace_path or not os.path.exists(trace_path):
        trace_path = os.path.join(run_dir, "telemetry_trace.jsonl")
    events = read_trace(trace_path)
    scheduled = [e for e in events if e.get("ev") == "scheduled"]
    attempts = [e for e in events if e.get("ev") == "attempt"]
    dropped = [e for e in events if e.get("ev") == "dropped"]

    # Unique IDs preserve the first scheduled time.
    sched = {}
    for e in scheduled:
        mid = e.get("messageId")
        if mid and mid not in sched:
            sched[mid] = e
    attempt_by_id = {}
    for e in attempts:
        mid = e.get("messageId")
        if mid and mid not in attempt_by_id:
            attempt_by_id[mid] = e
    ok_ids = {mid for mid, e in attempt_by_id.items()
              if e.get("ok") and e.get("http_status") == 201}

    if not phases:
        phases = [{"name": "run", "session": 0,
                   "start_epoch": min((e.get("wall_epoch", 0) for e in events), default=0),
                   "end_epoch": max((e.get("wall_epoch", 0) for e in events), default=0)}]
    start_epoch = min(float(p.get("start_epoch", 0)) for p in phases)
    end_epoch = max(float(p.get("end_epoch", 0)) for p in phases)
    query_end = end_epoch + args.grace_sec

    device_id = (args.device_id
                 or (manifest.get("config_values") or {}).get("TRANSITGUARD_DEVICE_ID")
                 or "BUS-101")

    report = {
        "run_dir": run_dir,
        "trace_path": trace_path,
        "device_id": device_id,
        "sessions": len([p for p in phases if p.get("session")]),
        "local_trace": {
            "events": len(events),
            "scheduled": len(scheduled),
            "scheduled_unique": len(sched),
            "attempted": len(attempts),
            "attempted_unique": len(attempt_by_id),
            "http_201_unique": len(ok_ids),
            "failed_attempts": sum(1 for e in attempts
                                   if not (e.get("ok") and e.get("http_status") == 201)),
            "dropped": len(dropped),
        },
        "query_window": {
            "start_utc": dt.datetime.fromtimestamp(start_epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": dt.datetime.fromtimestamp(query_end, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "grace_sec": args.grace_sec,
        },
        "supabase_checked": False,
        "note": ("HTTP 201 means the Edge Function accepted the packet; the "
                 "Supabase row check below is the delivery proof.  Late "
                 "buffer flushes beyond the grace window appear missing."),
    }

    if not args.supabase_url or not args.supabase_key:
        report["error"] = ("Supabase URL/key not supplied; set --supabase-url/"
                           "--supabase-key or SUPABASE_URL/SUPABASE_ANON_KEY")
        write_json(os.path.join(run_dir, "delivery_supabase.json"), report)
        print(report["error"])
        print(f"wrote {os.path.join(run_dir, 'delivery_supabase.json')}")
        return 0

    try:
        rows, url = supabase_rows(args.supabase_url, args.supabase_key, device_id,
                                  start_epoch - 30, query_end)
        report["supabase_checked"] = True
        report["query_url"] = url
        row_by_id = {r.get("message_id"): r for r in rows if r.get("message_id")}
        found = {mid for mid in sched if mid in row_by_id}
        missing = [mid for mid in sched if mid not in row_by_id]
        latencies = []
        for mid, row in row_by_id.items():
            if mid not in sched:
                continue
            a = attempt_by_id.get(mid)
            if not a:
                continue
            created = parse_iso(row.get("created_at"))
            attempted_end = a.get("wall_epoch")
            rtt = a.get("rtt_ms")
            if created is not None and attempted_end is not None:
                # Use the attempt start, not its end: the Edge Function inserts
                # the row before it sends the response, so end-to-end from the
                # response can be slightly negative.
                attempted_start = float(attempted_end)
                if rtt is not None:
                    attempted_start -= float(rtt) / 1000.0
                latencies.append((created - attempted_start) * 1000.0)
        session_report = {}
        for p in phases:
            if not p.get("session"):
                continue
            a, b = float(p.get("start_epoch", 0)), float(p.get("end_epoch", 0))
            ids = {mid for mid, e in sched.items()
                   if a <= float(e.get("wall_epoch") or 0) < b}
            session_report[str(p["session"])] = {
                "scheduled_unique": len(ids),
                "supabase_found": len(ids & found),
                "supabase_missing": len(ids - found),
                "http_201_unique": len(ids & ok_ids),
            }
        report.update({
            "supabase_rows_in_window": len(rows),
            "scheduled_found": len(found),
            "scheduled_missing": len(missing),
            "delivery_ratio": (round(len(found) / len(sched), 4) if sched else None),
            "http_201_found": len(ok_ids & found),
            "http_201_missing_row": len(ok_ids - found),
            "scheduled_not_attempted": len(set(sched) - set(attempt_by_id)),
            "latency_ms_median": median(latencies),
            "latency_ms_min": round(min(latencies), 1) if latencies else None,
            "latency_ms_max": round(max(latencies), 1) if latencies else None,
            "by_session": session_report,
            "missing_ids": missing[:50],
        })
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"

    out = os.path.join(run_dir, "delivery_supabase.json")
    write_json(out, report)
    summary_path = os.path.join(run_dir, "summary.json")
    summary = read_json(summary_path)
    if isinstance(summary, dict):
        summary["delivery_supabase"] = report
        write_json(summary_path, summary)

    print(f"local: scheduled={report['local_trace']['scheduled_unique']} "
          f"http201={report['local_trace']['http_201_unique']} "
          f"failed={report['local_trace']['failed_attempts']} "
          f"dropped={report['local_trace']['dropped']}")
    if report.get("error"):
        print(f"Supabase check failed: {report['error']}")
    else:
        print(f"Supabase: rows={report['supabase_rows_in_window']} "
              f"found={report['scheduled_found']}/{report['local_trace']['scheduled_unique']} "
              f"({report['delivery_ratio']}) missing={report['scheduled_missing']} "
              f"latency_median_ms={report['latency_ms_median']}")
        for k, v in report["by_session"].items():
            print(f"  session {k}: {v['supabase_found']}/{v['scheduled_unique']} delivered")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
