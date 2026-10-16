#!/usr/bin/env python3
"""Export one row per attempted telemetry message, reconciled with Supabase."""
import csv
import datetime as dt
import json
import os
import urllib.parse
import urllib.request

# Publishable (client-safe) values, matching the dashboard's built-in fallbacks.
URL = os.environ.get("SUPABASE_URL", "https://hfzqfcuezuxcgaifxqok.supabase.co")
K = os.environ.get(
    "SUPABASE_PUBLISHABLE_KEY", "sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF")


def epoch(iso):
    s = iso.replace("Z", "+00:00")
    if "." in s:
        head, rest = s.split(".", 1)
        frac, off = rest, ""
        for m in ("+", "-"):
            i = rest.find(m)
            if i != -1:
                frac, off = rest[:i], rest[i:]
                break
        s = f"{head}.{(frac + '000000')[:6]}{off}"
    return dt.datetime.fromisoformat(s).timestamp()


def fetch_all(start_iso):
    rows, off, page = [], 0, 1000
    while True:
        q = urllib.parse.urlencode({
            "select": "message_id,created_at,timestamp",
            "device_id": "eq.BUS-101",
            "created_at": f"gte.{start_iso}",
            "order": "created_at.asc", "limit": page, "offset": off,
        })
        req = urllib.request.Request(f"{URL}/rest/v1/telemetry?{q}",
                                    headers={"apikey": K, "Authorization": f"Bearer {K}"})
        batch = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        rows.extend(batch)
        if len(batch) < page:
            return rows
        off += page


ev = [json.loads(l) for l in open("telemetry_trace.jsonl", encoding="utf-8") if l.strip()]
sched = {e["messageId"]: e for e in ev if e.get("ev") == "scheduled"}
att = [e for e in ev if e.get("ev") == "attempt"]
t0 = min(e["wall_epoch"] for e in ev if "wall_epoch" in e)
start_iso = dt.datetime.fromtimestamp(t0 - 120, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
rows = {r["message_id"]: r for r in fetch_all(start_iso)}

seen, out = {}, []
for a in sorted(att, key=lambda x: x["wall_epoch"]):
    mid = a.get("messageId")
    seen[mid] = seen.get(mid, 0) + 1
    s = sched.get(mid, {})
    row = rows.get(mid)
    e2e = ""
    if row and a.get("device_ts_ms"):
        e2e = round((epoch(row["created_at"]) - a["device_ts_ms"] / 1000.0) * 1000.0, 1)
    out.append({
        "message_id": mid,
        "trigger": s.get("trigger", ""),
        "driver_state": s.get("driver_state", ""),
        "scheduled_utc": s.get("wall_utc", ""),
        "attempt_utc": a.get("wall_utc", ""),
        "source": a.get("source", ""),
        "rtt_ms": a.get("rtt_ms", ""),
        "http_status": a.get("http_status", ""),
        "error": a.get("error") or "",
        "stored_in_db": "yes" if row else "no",
        "server_created_at": row["created_at"] if row else "",
        "e2e_ms": e2e,
        "duplicate_attempt": "yes" if seen[mid] > 1 else "no",
    })

with open("session_messages.csv", "w", newline="", encoding="utf-8") as fh:
    w = csv.DictWriter(fh, fieldnames=list(out[0].keys()))
    w.writeheader()
    w.writerows(out)
print(f"wrote session_messages.csv ({len(out)} rows)")
