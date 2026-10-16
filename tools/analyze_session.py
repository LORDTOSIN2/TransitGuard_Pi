#!/usr/bin/env python3
"""
TransitGuard connection-stability analysis.

Reconciles the Pi-side telemetry trace (JSONL, written by vehicle_monitor.py
when TELEMETRY_TRACE_PATH is set) against the authoritative rows in the
Supabase `public.telemetry` table, and reports:

  scheduled / attempted / accepted / failed / duplicate counts per messageId
  HTTP round-trip time (from time.monotonic() on the Pi), reported separately
  from end-to-end delay (server created_at - device timestamp)
  delivery ratio = accepted unique messages / attempted messages

Clock note: the Pi runs systemd-timesyncd (NTP, stratum 2, offset ~+15 ms) and
`created_at` is Postgres now() on Supabase's NTP-synced host, so end-to-end is
reportable; clock skew is also estimated from the HTTP Date header.

Usage:
    python analyze_session.py --trace telemetry_trace.jsonl [--csv out.csv]
"""
import argparse
import collections
import datetime as dt
import json
import math
import os
import statistics
import urllib.error
import urllib.parse
import urllib.request

# Publishable (client-safe) values, matching the dashboard's built-in fallbacks.
# Override with the usual TransitGuard env vars if your project differs.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://hfzqfcuezuxcgaifxqok.supabase.co")
PUBLISHABLE_KEY = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF")
DEVICE_ID = os.environ.get("TRANSITGUARD_DEVICE_ID", "BUS-101")

SELECT_COLS = ",".join([
    "message_id", "device_id", "created_at", "timestamp",
    "transport_mode", "wifi_ssid", "wifi_rssi", "driver_state",
])


def epoch(iso):
    """Parse a Postgres/ISO timestamp into epoch seconds."""
    if iso is None:
        return None
    s = iso.replace("Z", "+00:00")
    # Postgres can emit more than 6 fractional digits; trim to microseconds.
    if "." in s:
        head, rest = s.split(".", 1)
        frac = rest
        offset = ""
        for marker in ("+", "-"):
            idx = rest.find(marker)
            if idx != -1:
                frac, offset = rest[:idx], rest[idx:]
                break
        frac = (frac + "000000")[:6]
        s = f"{head}.{frac}{offset}"
    return dt.datetime.fromisoformat(s).timestamp()


def fetch_rows(start_iso, end_iso):
    """Page through PostgREST for telemetry rows received in the window."""
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
                     "Authorization": f"Bearer {PUBLISHABLE_KEY}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                batch = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Supabase read failed: HTTP {e.code} {e.read()[:300]!r}")
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return [r for r in rows if epoch(r["created_at"]) <= epoch(end_iso) + 1]


def pctl(values, p):
    if not values:
        return None
    vs = sorted(values)
    k = (len(vs) - 1) * (p / 100.0)
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return vs[int(k)]
    return vs[lo] + (vs[hi] - vs[lo]) * (k - lo)


def load_trace(path):
    events = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--bucket-min", type=float, default=5.0)
    args = ap.parse_args()

    events = load_trace(args.trace)
    scheduled = [e for e in events if e.get("ev") == "scheduled"]
    attempts = [e for e in events if e.get("ev") == "attempt"]
    dropped = [e for e in events if e.get("ev") == "dropped"]
    if not events:
        raise SystemExit("Trace is empty.")

    wall = [e["wall_epoch"] for e in events if "wall_epoch" in e]
    t_start, t_end = min(wall), max(wall)
    start_iso = dt.datetime.fromtimestamp(t_start - 120, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")
    end_iso = dt.datetime.fromtimestamp(t_end + 120, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")

    print(f"Session window : {dt.datetime.fromtimestamp(t_start, dt.timezone.utc):%Y-%m-%d %H:%M:%S} "
          f"-> {dt.datetime.fromtimestamp(t_end, dt.timezone.utc):%H:%M:%S} UTC "
          f"({(t_end - t_start) / 60.0:.1f} min)")
    print(f"Trace events   : {len(events)}")

    rows = fetch_rows(start_iso, end_iso)
    stored_ids = {r["message_id"] for r in rows}

    attempted_ids = [a.get("messageId") for a in attempts]
    distinct_attempted = set(i for i in attempted_ids if i)
    ok_201 = [a for a in attempts if a.get("http_status") == 201]
    failures = [a for a in attempts if a.get("http_status") != 201]
    responded = [a for a in attempts if a.get("http_status") is not None]

    # messageIds that came back 201 but are not in the table (should be none)
    unmatched = distinct_attempted - stored_ids
    # attempts that were re-sends of an id already attempted (duplicate attempts)
    retries = len(attempted_ids) - len(distinct_attempted)

    sched_ids = [s.get("messageId") for s in scheduled]
    never_attempted = set(i for i in sched_ids if i) - distinct_attempted

    print()
    print("=" * 66)
    print("COUNTS")
    print("=" * 66)
    print(f"  scheduled (queued)          : {len(scheduled)}")
    print(f"  dropped from queue (oldest) : {len(dropped)}")
    print(f"  attempted (HTTP POSTs)      : {len(attempts)}")
    print(f"  attempted, distinct msgIds  : {len(distinct_attempted)}")
    print(f"  accepted by client (HTTP201): {len(ok_201)}")
    print(f"  stored server-side (unique) : {len(stored_ids & distinct_attempted)}")
    print(f"  failed (non-201 / timeout)  : {len(failures)}")
    print(f"  duplicate attempts (re-sends): {retries}")
    print(f"  scheduled but never attempted: {len(never_attempted)}")
    print(f"  attempted 201 but not stored : {len(unmatched)}")
    print(f"  rows in table for window     : {len(rows)}")

    n_att = len(attempts)
    acc_unique = len(stored_ids & distinct_attempted)
    ratio = (acc_unique / n_att * 100.0) if n_att else 0.0
    print()
    print(f"  DELIVERY RATIO (accepted unique / attempted) = {acc_unique}/{n_att} = {ratio:.2f}%")

    if failures:
        print()
        print("  Failure breakdown:")
        for (status, err), n in collections.Counter(
                (f.get("http_status"), (f.get("error") or "")[:60]) for f in failures).most_common():
            print(f"    HTTP {status} / {err!r}: {n}")

    print()
    print("=" * 66)
    print("HTTP ROUND-TRIP TIME (Pi monotonic timer, request->response)")
    print("=" * 66)
    rtts = [a["rtt_ms"] for a in responded if a.get("rtt_ms") is not None]
    if rtts:
        print(f"  n={len(rtts)}  min={min(rtts):.0f}ms  mean={statistics.mean(rtts):.0f}ms  "
              f"median={statistics.median(rtts):.0f}ms  p95={pctl(rtts, 95):.0f}ms  max={max(rtts):.0f}ms")
        if len(rtts) > 1:
            print(f"  stdev={statistics.stdev(rtts):.0f}ms")
    else:
        print("  (no responses)")

    print()
    print("=" * 66)
    print("END-TO-END DELAY (server created_at - device timestamp)")
    print("=" * 66)
    e2e = []
    by_id = {r["message_id"]: r for r in rows}
    for a in attempts:
        mid = a.get("messageId")
        dev_ms = a.get("device_ts_ms")
        row = by_id.get(mid)
        if row and dev_ms:
            created = epoch(row["created_at"])
            if created:
                e2e.append((created - dev_ms / 1000.0) * 1000.0)
    if e2e:
        print(f"  n={len(e2e)}  min={min(e2e):.0f}ms  mean={statistics.mean(e2e):.0f}ms  "
              f"median={statistics.median(e2e):.0f}ms  p95={pctl(e2e, 95):.0f}ms  max={max(e2e):.0f}ms")
        print("  (includes queue wait + HTTP RTT; clocks are NTP-synced on both sides)")
    else:
        print("  (no matched rows)")

    print()
    print("=" * 66)
    print("CLOCK SKEW (server Date header vs Pi clock, RTT/2 corrected)")
    print("=" * 66)
    skews = []
    for a in attempts:
        sd = a.get("server_date")
        if not sd:
            continue
        try:
            srv = dt.datetime.strptime(sd, "%a, %d %b %Y %H:%M:%S GMT").replace(
                tzinfo=dt.timezone.utc).timestamp()
        except ValueError:
            continue
        # wall_epoch is captured after the response; request start ~= wall - rtt.
        local_mid = a["wall_epoch"] - (a.get("rtt_ms", 0) / 3000.0)
        skews.append(srv - local_mid)
    if skews:
        print(f"  n={len(skews)}  mean={statistics.mean(skews) * 1000:+.0f}ms  "
              f"median={statistics.median(skews) * 1000:+.0f}ms   (Date header is 1 s resolution)")
        print(f"  => Pi clock is {'ahead of' if statistics.mean(skews) < 0 else 'behind'} "
              f"Supabase's edge by ~{abs(statistics.mean(skews)) * 1000:.0f}ms")
    else:
        print("  (no Date headers captured)")

    print()
    print("=" * 66)
    print(f"STABILITY OVER TIME ({args.bucket_min:g}-minute buckets)")
    print("=" * 66)
    buckets = collections.defaultdict(lambda: {"att": 0, "ok": 0, "rtt": [], "e2e": []})
    for a in attempts:
        idx = int((a["wall_epoch"] - t_start) // (args.bucket_min * 60))
        b = buckets[idx]
        b["att"] += 1
        if a.get("http_status") == 201:
            b["ok"] += 1
        if a.get("rtt_ms") is not None:
            b["rtt"].append(a["rtt_ms"])
        row = by_id.get(a.get("messageId"))
        if row and a.get("device_ts_ms"):
            created = epoch(row["created_at"])
            if created:
                b["e2e"].append((created - a["device_ts_ms"] / 1000.0) * 1000.0)
    print(f"  {'start(UTC)':<16} {'att':>4} {'ok':>4} {'fail':>5} {'rtt_med':>8} {'e2e_med':>8}")
    for idx in sorted(buckets):
        b = buckets[idx]
        ts = dt.datetime.fromtimestamp(t_start + idx * args.bucket_min * 60,
                                      dt.timezone.utc).strftime("%H:%M")
        rmed = f"{statistics.median(b['rtt']):.0f}ms" if b["rtt"] else "-"
        emed = f"{statistics.median(b['e2e']):.0f}ms" if b["e2e"] else "-"
        print(f"  {ts:<16} {b['att']:>4} {b['ok']:>4} {b['att'] - b['ok']:>5} {rmed:>8} {emed:>8}")

    # Cadence check
    sched_ts = sorted(s["wall_epoch"] for s in scheduled if "wall_epoch" in s)
    if len(sched_ts) > 2:
        gaps = [b - a for a, b in zip(sched_ts, sched_ts[1:])]
        gaps = [g for g in gaps if g < 120]
        print()
        print(f"  Scheduling cadence: median gap {statistics.median(gaps):.2f}s "
              f"(min {min(gaps):.2f}s, max {max(gaps):.2f}s) vs 10s configured")

    summary = {
        "window_start_utc": dt.datetime.fromtimestamp(t_start, dt.timezone.utc).isoformat(),
        "window_end_utc": dt.datetime.fromtimestamp(t_end, dt.timezone.utc).isoformat(),
        "duration_min": round((t_end - t_start) / 60.0, 2),
        "scheduled": len(scheduled), "dropped": len(dropped),
        "attempted": n_att, "attempted_unique": len(distinct_attempted),
        "accepted_http201": len(ok_201), "stored_unique": acc_unique,
        "failed": len(failures), "duplicate_attempts": retries,
        "scheduled_never_attempted": len(never_attempted),
        "delivery_ratio_pct": round(ratio, 2),
        "rtt_ms": {
            "n": len(rtts), "min": round(min(rtts), 1) if rtts else None,
            "mean": round(statistics.mean(rtts), 1) if rtts else None,
            "median": round(statistics.median(rtts), 1) if rtts else None,
            "p95": round(pctl(rtts, 95), 1) if rtts else None,
            "max": round(max(rtts), 1) if rtts else None,
        },
        "e2e_ms": {
            "n": len(e2e), "mean": round(statistics.mean(e2e), 1) if e2e else None,
            "median": round(statistics.median(e2e), 1) if e2e else None,
            "p95": round(pctl(e2e, 95), 1) if e2e else None,
        },
        "clock_skew_ms_mean": round(statistics.mean(skews) * 1000, 0) if skews else None,
    }
    with open("session_summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print("\nWrote session_summary.json")


if __name__ == "__main__":
    main()
