#!/usr/bin/env python3
"""Render the connection-stability charts for the TransitGuard 30-min run."""
import datetime as dt
import json
import os
import statistics
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

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


def count_window(start_iso, end_iso):
    """Exact count of distinct messageIds in [start,end] using Range + count=exact."""
    q = urllib.parse.urlencode({
        "select": "message_id",
        "device_id": "eq.BUS-101",
        "and": f"(created_at.gte.{start_iso},created_at.lte.{end_iso})",
    })
    req = urllib.request.Request(f"{URL}/rest/v1/telemetry?{q}",
                                 headers={"apikey": K, "Authorization": f"Bearer {K}",
                                          "Prefer": "count=exact", "Range": "0-0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("Content-Range", "")
    return cr


ev = [json.loads(l) for l in open("telemetry_trace.jsonl", encoding="utf-8") if l.strip()]
att = [e for e in ev if e.get("ev") == "attempt"]
t0 = min(e["wall_epoch"] for e in ev if "wall_epoch" in e)
end = max(e["wall_epoch"] for e in ev if "wall_epoch" in e)

start_iso = dt.datetime.fromtimestamp(t0 - 120, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
end_iso = dt.datetime.fromtimestamp(end + 120, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
print("Content-Range for BUS-101 created_at in window:", count_window(start_iso, end_iso))

rows = {r["message_id"]: r for r in fetch_all(start_iso)}

mins, rtt, e2e, ok = [], [], [], []
for a in att:
    mins.append((a["wall_epoch"] - t0) / 60.0)
    r = a.get("rtt_ms")
    rtt.append(r if r is not None else float("nan"))
    ok.append(1 if a.get("http_status") == 201 else 0)
    row = rows.get(a.get("messageId"))
    if row and a.get("device_ts_ms"):
        e2e.append((epoch(row["created_at"]) - a["device_ts_ms"] / 1000.0) * 1000.0)
    else:
        e2e.append(float("nan"))

fig, ax = plt.subplots(2, 2, figsize=(14, 8))
fig.suptitle("TransitGuard telemetry upload - 30-minute stability run "
             "(10 s cadence, Wi-Fi, 179 packets)", fontsize=13, fontweight="bold")

a0 = ax[0][0]
a0.plot(mins, rtt, marker="o", ms=3, lw=0.8, color="#2563eb")
a0.axhline(statistics.median([x for x in rtt if x == x]), color="#dc2626", ls="--", lw=1,
           label=f"median {statistics.median([x for x in rtt if x == x]):.0f} ms")
a0.set_title("HTTP round-trip time (Pi monotonic timer)")
a0.set_xlabel("minutes into run"); a0.set_ylabel("RTT (ms)")
a0.legend(fontsize=8); a0.grid(alpha=.3)

a1 = ax[0][1]
a1.plot(mins, e2e, marker="o", ms=3, lw=0.8, color="#059669")
a1.set_title("End-to-end delay (server created_at - device timestamp)")
a1.set_xlabel("minutes into run"); a1.set_ylabel("delay (ms)")
a1.grid(alpha=.3)

a2 = ax[1][0]
vals = [x for x in rtt if x == x]
a2.hist(vals, bins=30, color="#7c3aed", alpha=.85, edgecolor="white")
a2.set_title(f"RTT distribution (n={len(vals)})")
a2.set_xlabel("RTT (ms)"); a2.set_ylabel("count")
a2.grid(alpha=.3)

a3 = ax[1][1]
bucket = 5
labels, med = [], []
b = {}
for m, r in zip(mins, rtt):
    if r == r:
        b.setdefault(int(m // bucket), []).append(r)
for k in sorted(b):
    labels.append(f"{int(k * bucket)}-{int(k * bucket) + bucket}")
    med.append(statistics.median(b[k]))
a3.bar(labels, med, color="#0ea5e9")
a3.set_title("Median RTT per 5-minute bucket")
a3.set_ylabel("median RTT (ms)"); a3.tick_params(axis="x", rotation=45)
a3.grid(alpha=.3, axis="y")

plt.tight_layout(rect=[0, 0, 1, 0.95])
plt.savefig("session_charts.png", dpi=110)
print("wrote session_charts.png")
