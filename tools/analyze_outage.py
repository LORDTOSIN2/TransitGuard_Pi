#!/usr/bin/env python3
"""Reconcile the v2 connection-loss & recovery trials.

Inputs (produced by run_outage_trials_v2.sh and pulled off the Pi):
  * outage_poll.csv              - buffer / network / temp / blackhole count every 5 s
  * outage_summary.txt           - authoritative CUT/RESTORE/recovery verdicts
  * telemetry_trace_trials2.jsonl- scheduled / attempt / dropped events
Server side:
  * Supabase `telemetry` rows (PostgREST, publishable key)

Usage:
  python analyze_outage_v2.py [POLL] [SUMMARY] [TRACE] [OUT_JSON]

Notes:
  * v2 has no `sent_since_restore` column and does not use `cut`; recovery is
    taken from the harness summary + the trace.
  * A duplicate upsert returns HTTP 201 and inserts nothing, so "accepted" is
    server-derived (message_id present in the telemetry table).
"""
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse
import urllib.request

URL = os.environ.get("SUPABASE_URL", "https://hfzqfcuezuxcgaifxqok.supabase.co")
KEY = os.environ.get("SUPABASE_PUBLISHABLE_KEY",
                     "sb_publishable_0DseTT82noxmCUmx74miOg_yGFH71TF")
DEVICE = "BUS-101"

POLL = sys.argv[1] if len(sys.argv) > 1 else "outage_poll.csv"
SUMMARY = sys.argv[2] if len(sys.argv) > 2 else "outage_summary.txt"
TRACE = sys.argv[3] if len(sys.argv) > 3 else "telemetry_trace_outage.jsonl"
OUT = sys.argv[4] if len(sys.argv) > 4 else "outage_summary.json"


def iso_utc(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+00:00")


def iso_to_epoch(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def fetch(start_iso, end_iso):
    rows, off = [], 0
    while True:
        params = [
            ("select", "message_id,created_at"),
            ("device_id", f"eq.{DEVICE}"),
            ("created_at", f"gte.{start_iso}"),
            ("created_at", f"lte.{end_iso}"),
            ("order", "created_at.asc"),
            ("limit", "1000"),
            ("offset", str(off)),
        ]
        req = urllib.request.Request(
            f"{URL}/rest/v1/telemetry?{urllib.parse.urlencode(params)}",
            headers={"apikey": KEY, "Authorization": f"Bearer {KEY}"})
        batch = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
        rows.extend(batch)
        if len(batch) < 1000:
            return rows
        off += 1000


# ---- poll CSV -----------------------------------------------------------
samples = []
with open(POLL, newline="", encoding="utf-8") as fh:
    for r in csv.DictReader(fh):
        samples.append(r)

trials = {}
for s in samples:
    trials.setdefault(int(s["trial"]), []).append(s)


def phase(rows, name):
    return [r for r in rows if r["phase"] == name]


def ints(rows, key):
    out = []
    for r in rows:
        v = r.get(key)
        if v not in ("", None):
            try:
                out.append(int(float(v)))
            except ValueError:
                pass
    return out


# ---- harness summary ----------------------------------------------------
sumtxt = open(SUMMARY, encoding="utf-8", errors="ignore").read() \
    if os.path.exists(SUMMARY) else ""
meta = {}
for k, pat in [
    ("app_head", r"app HEAD[ \t]*:[ \t]*([^\r\n]*)"),
    ("config_sha256", r"config sha256\(env\)[ \t]*:[ \t]*([^\r\n]*)"),
    ("trace_file", r"trace file[ \t]*:[ \t]*([^\r\n]*)"),
    ("ingest_host", r"ingest host[ \t]*:[ \t]*([^\r\n]*)"),
    ("resolves_to", r"host resolves to[ \t]*:[ \t]*([^\r\n]+)"),
    ("timing", r"timing[ \t]*:[ \t]*([^\r\n]+)"),
    ("run_complete", r"=== run complete ([^\r\n]+?)\s+blackhole"),
]:
    m = re.search(pat, sumtxt)
    meta[k] = m.group(1).strip() if m else ""


def sgrab(pat, trial):
    m = re.search(pat, sumtxt)
    return m


summ = {}
for m in re.finditer(r"TRIAL (\d+)\s+([^\n]+)", sumtxt):
    t = int(m.group(1))
    summ.setdefault(t, []).append(m.group(2).strip())

verdicts = {}
probes = {}
for t, lines in summ.items():
    for ln in lines:
        m = re.match(r"CUT\s+(\S+)\s+probe_http=(\S+)\s+blackhole_routes=(\d+)", ln)
        if m:
            probes.setdefault(t, {})["cut_utc"] = m.group(1)
            probes[t]["cut_probe"] = m.group(2)
            probes[t]["cut_bh"] = int(m.group(3))
        m = re.match(r"RESTORE\s+(\S+)\s+probe_http=(\S+)\s+blackhole_routes=(\d+)", ln)
        if m:
            probes.setdefault(t, {})["restore_utc"] = m.group(1)
            probes[t]["restore_probe"] = m.group(2)
            probes[t]["restore_bh"] = int(m.group(3))
        m = re.match(r"cut verified", ln)
        if m:
            probes.setdefault(t, {})["cut_verified"] = True
        m = re.match(r"restore verified \(block lifted\)", ln)
        if m:
            probes.setdefault(t, {})["restore_verified"] = True
        m = re.match(r"recovery: first 201 at t\+(\S+)s \| first LIVE 201 at t\+(\S+)s "
                     r"\| queue first empty at t\+(\S+)s \| remaining=(\S+)", ln)
        if m:
            probes.setdefault(t, {})["first_201_s"] = m.group(1)
            probes[t]["first_live_201_s"] = m.group(2)
            probes[t]["clear_s"] = m.group(3)
            probes[t]["remaining_after"] = m.group(4)
        m = re.match(r"post-restore 201s: total=(\d+) live=(\d+)", ln)
        if m:
            probes.setdefault(t, {})["post_201_total"] = int(m.group(1))
            probes[t]["post_201_live"] = int(m.group(2))
        m = re.match(r"RECOVERY (CONFIRMED|NOT CONFIRMED)", ln)
        if m:
            probes.setdefault(t, {})["verdict"] = m.group(1)
        m = re.match(r"buffered during outage: peak=(\d+) at-restore=(\d+)", ln)
        if m:
            probes.setdefault(t, {})["peak"] = int(m.group(1))
            probes[t]["at_restore"] = int(m.group(2))
        m = re.match(r"early stop at t\+(\d+)s", ln)
        if m:
            probes.setdefault(t, {})["early_stop_s"] = int(m.group(1))

# ---- trace --------------------------------------------------------------
events = []
with open(TRACE, encoding="utf-8", errors="ignore") as fh:
    for line in fh:
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                pass
sched = [e for e in events if e.get("ev") == "scheduled"]
attempts = [e for e in events if e.get("ev") == "attempt"]
dropped = [e for e in events if e.get("ev") == "dropped"]

# ---- per trial ----------------------------------------------------------
plan = []
for t in sorted(trials):
    rows = trials[t]
    during = phase(rows, "during")
    after = phase(rows, "after")
    before = phase(rows, "before")
    cut = int(during[0]["epoch"]) if during else None
    restore = int(after[0]["epoch"]) if after else (
        int(during[-1]["epoch"]) if during else None)
    end = int(after[-1]["epoch"]) if after else restore
    plan.append(dict(trial=t, cut=cut, restore=restore, end=end,
                     before=before, during=during, after=after))

lo = min(p["cut"] for p in plan) - 120
hi = max(p["end"] for p in plan) + 300
db_rows = fetch(iso_utc(lo), iso_utc(hi))
db_ids = {r["message_id"] for r in db_rows}

report = {"device": DEVICE, "method": "ingest-scoped blackhole", "meta": meta,
          "trials": []}
for i, p in enumerate(plan):
    t, cut, restore, end = p["trial"], p["cut"], p["restore"], p["end"]
    next_cut = plan[i + 1]["cut"] if i + 1 < len(plan) else hi
    depths = ints(p["during"], "buffer_count")
    af = ints(p["after"], "buffer_count")
    temps = ints(p["during"], "temp_c") + ints(p["before"], "temp_c") + \
        ints(p["after"], "temp_c")

    sched_win = [e for e in sched if cut <= (e.get("wall_epoch") or 0) <= restore]
    queued_ids = [e.get("messageId") for e in sched_win if e.get("messageId")]
    att_win = [e for e in attempts if cut <= (e.get("wall_epoch") or 0) <= restore]
    att_after = [e for e in attempts
                 if restore < (e.get("wall_epoch") or 0) < next_cut]
    flush_ok = [e for e in att_after if e.get("source") == "flush" and e.get("ok")]
    live_ok = [e for e in att_after if e.get("source") == "live" and e.get("ok")]
    accepted = [m for m in queued_ids if m in db_ids]
    dropped_win = [e for e in dropped if cut <= (e.get("wall_epoch") or 0) <= restore]
    rtts = sorted(e["rtt_ms"] for e in (flush_ok + live_ok)
                  if e.get("rtt_ms") is not None)

    pr = probes.get(t, {})
    # ---- corrected recovery verdict: ANY live 201 after the queue emptied ----
    # (the harness v2 expression compared the FIRST live-201 epoch to the clear
    #  time, so a live 201 landing before clear masked all later ones.)
    zero_rows = [int(r["epoch"]) for r in p["after"]
                 if r.get("buffer_count") not in ("", None)
                 and r["buffer_count"] == "0"]
    clear_epoch = zero_rows[0] if zero_rows else None
    live_after_clear = sorted(int(e["wall_epoch"]) for e in live_ok
                              if clear_epoch is not None
                              and (e.get("wall_epoch") or 0) > clear_epoch)
    verdict = ("CONFIRMED" if (clear_epoch is not None and live_after_clear)
               else "NOT CONFIRMED")
    report["trials"].append(dict(
        trial=t,
        cut_utc=iso_utc(cut), restore_utc=iso_utc(restore),
        cut_probe=pr.get("cut_probe"), cut_bh=pr.get("cut_bh"),
        cut_verified=pr.get("cut_verified", False),
        restore_probe=pr.get("restore_probe"), restore_bh=pr.get("restore_bh"),
        restore_verified=pr.get("restore_verified", False),
        pending_before=int(p["before"][0]["buffer_count"]) if p["before"] else None,
        peak_during=max(depths) if depths else pr.get("peak"),
        at_restore=pr.get("at_restore", depths[-1] if depths else None),
        scheduled_during_outage=len(sched_win),
        attempts_during_outage=len(att_win),
        queued_unique=len(set(queued_ids)),
        replayed_ok=len(flush_ok),
        live_ok_after=len(live_ok),
        accepted_unique=len(set(accepted)),
        dropped=len(dropped_win),
        remaining_after=pr.get("remaining_after"),
        time_to_first_201_s=pr.get("first_201_s"),
        time_to_first_live_201_s=pr.get("first_live_201_s"),
        time_to_queue_zero_s=pr.get("clear_s"),
        post_201_total=pr.get("post_201_total"),
        post_201_live=pr.get("post_201_live"),
        early_stop_s=pr.get("early_stop_s"),
        harness_verdict=pr.get("verdict"),
        clear_epoch=clear_epoch,
        live_201_after_clear=len(live_after_clear),
        live_201_after_clear_rel_s=([x - restore for x in live_after_clear]
                                    if restore else []),
        verdict=verdict,
        temp_min_c=min(temps) if temps else None,
        temp_max_c=max(temps) if temps else None,
        rtt_ms_post_recovery=rtts,
    ))

# ---- totals -------------------------------------------------------------
tot = dict(
    trials=len(report["trials"]),
    cut_verified=sum(1 for t in report["trials"] if t["cut_verified"]),
    restore_verified=sum(1 for t in report["trials"] if t["restore_verified"]),
    recovered=sum(1 for t in report["trials"] if t["verdict"] == "CONFIRMED"),
    harness_recovered=sum(1 for t in report["trials"]
                          if t["harness_verdict"] == "CONFIRMED"),
    scheduled=sum(t["scheduled_during_outage"] for t in report["trials"]),
    replayed_ok=sum(t["replayed_ok"] for t in report["trials"]),
    accepted_unique=sum(t["accepted_unique"] for t in report["trials"]),
    dropped=sum(t["dropped"] for t in report["trials"]),
    db_rows_in_window=len(db_rows),
)
report["totals"] = tot

with open(OUT, "w", encoding="utf-8") as fh:
    json.dump(report, fh, indent=2)

# ---- print --------------------------------------------------------------
print(f"trace events: {len(events)}  (sched={len(sched)} attempt={len(attempts)} "
      f"dropped={len(dropped)})")
print(f"supabase rows in window: {len(db_rows)} (device {DEVICE})")
if meta.get("app_head"):
    print(f"app HEAD {meta['app_head']}  config sha256 {meta['config_sha256']}")
print()
hdr = (f"{'T':>2} {'cutV':>4} {'rstV':>4} {'before':>6} {'peak':>5} {'@rest':>5} "
       f"{'sched':>5} {'att':>4} {'queued':>6} {'replay':>6} {'accept':>6} "
       f"{'drop':>4} {'1stLive':>7} {'clear':>5} {'liveAC':>6} "
       f"{'harness':>13} {'corrected':>13}")
print(hdr)
for t in report["trials"]:
    print(f"{t['trial']:>2} {'Y' if t['cut_verified'] else 'N':>4} "
          f"{'Y' if t['restore_verified'] else 'N':>4} "
          f"{str(t['pending_before']):>6} {str(t['peak_during']):>5} "
          f"{str(t['at_restore']):>5} {t['scheduled_during_outage']:>5} "
          f"{t['attempts_during_outage']:>4} {t['queued_unique']:>6} "
          f"{t['replayed_ok']:>6} {t['accepted_unique']:>6} {t['dropped']:>4} "
          f"{str(t['time_to_first_live_201_s']):>7} "
          f"{str(t['time_to_queue_zero_s']):>5} {str(t['live_201_after_clear']):>6} "
          f"{str(t['harness_verdict']):>13} {t['verdict']:>13}")
print()
print("totals:", json.dumps(tot))
print(f"wrote {OUT}")
