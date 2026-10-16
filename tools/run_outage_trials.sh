#!/bin/sh
# TransitGuard connection-loss & recovery trials -- v2 (hardened) 2026-10-16
#
# Ingest-scoped WAN blackhole: only the Supabase/Cloudflare routes are dropped,
# so SSH + the dashboard (both on-link, never via the default route) stay up.
# 3 trials x 5 min outage; after restore, observe up to 3 min, stopping early
# once the SQLite buffer is empty AND a fresh live HTTP 201 has been recorded.
#
# LoRa is a point-to-point UART radio with no internet path; left as-is, NOT tested.
#
# v2.1 post-run fix: the recovery early-stop previously compared the FIRST live
#   201 epoch against the clear time, so a live 201 that landed *before* the
#   buffer emptied masked later ones and every trial read NOT CONFIRMED. The
#   test now uses the LAST live-ok epoch (i.e. "any live 201 after clearance").
#   Only the verdict expression changed; no measurement logic. The 2026-10-16
#   run used the v2 expression, so its verdicts were re-derived from the trace.
#
# v2 changes vs v1:
#   * ingest host read from /etc/transitguard.env (no hard-coded host)
#   * no use of `cut` (it returns nothing in this environment) -- awk/python only
#   * explicit blackhole-route assertions + reachability probe at cut & restore
#   * restore is guaranteed: leftovers are removed and reported
#   * absolute wall-clock cut/observe windows (no drift from sampling overhead)
#   * recovery detected from the telemetry trace JSONL (live + flush 201s)
#   * leak check during the cut + late /32 re-add if DNS rotates
#   * cleanup on EXIT/INT/TERM
set -u

APP=/home/john/fatigue
LOGDIR=$APP/logs
POLL=$LOGDIR/outage_poll.csv
SUM=$LOGDIR/outage_summary.txt
TRACE=$LOGDIR/telemetry_trace_trials2.jsonl
DONE=$LOGDIR/outage_DONE
CUT32=$LOGDIR/.outage_cut32
LEAK=$LOGDIR/outage_leaks.txt
CUT_SECS=300
OBS_MAX=180
POLL_SECS=5
DEAD_SECS=480

rm -f "$DONE"
: > "$CUT32"
: > "$LEAK"

# --- authoritative ingest host from the app config ---
INGEST_URL=$(sudo -n grep -E '^SUPABASE_INGEST_URL=' /etc/transitguard.env 2>/dev/null | head -1)
INGEST_URL=${INGEST_URL#*=}
HOST=$(printf '%s' "$INGEST_URL" | sed -E 's#^https?://([^/]+).*$#\1#')
[ -n "$HOST" ] || HOST=hfzqfcuezuxcgaifxqok.supabase.co

# Broad Cloudflare ranges that Supabase sits behind (belt and braces).
NETS="104.18.0.0/16 172.64.0.0/16"

bh_count() { /usr/bin/ip route show 2>/dev/null | awk '/^blackhole/{n++} END{print n+0}'; }
resolved_ips() { getent ahostsv4 "$HOST" 2>/dev/null | awk '{print $1}' | sort -u; }
reach() { curl -s -m 4 -o /dev/null -w '%{http_code}' "https://$HOST/rest/v1/" 2>/dev/null; }
jget() { printf '%s' "$1" | python3 -c "import sys,json;print(json.load(sys.stdin).get('$2'))" 2>/dev/null; }

add_resolved() {
  for ip in $(resolved_ips); do
    if ! grep -qxF "$ip" "$CUT32" 2>/dev/null; then
      sudo -n /usr/bin/ip route add blackhole "$ip/32" 2>/dev/null && echo "$ip" >> "$CUT32"
    fi
  done
}
cut() {
  for n in $NETS; do sudo -n /usr/bin/ip route add blackhole "$n" 2>/dev/null; done
  add_resolved
}
uncut() {
  for n in $NETS; do sudo -n /usr/bin/ip route del blackhole "$n" 2>/dev/null; done
  if [ -s "$CUT32" ]; then
    for ip in $(cat "$CUT32"); do sudo -n /usr/bin/ip route del blackhole "$ip/32" 2>/dev/null; done
  fi
  : > "$CUT32"
  # Guarantee: sweep any blackhole route still present (and report it).
  left=$(bh_count)
  if [ "$left" != 0 ]; then
    echo "TRIAL-RESTORE WARNING: $left blackhole route(s) survived targeted removal; sweeping" | tee -a "$SUM"
    for dst in $(/usr/bin/ip route show | awk '/^blackhole/{print $2}'); do
      case "$dst" in */*) sudo -n /usr/bin/ip route del blackhole "$dst" 2>/dev/null ;; *) sudo -n /usr/bin/ip route del blackhole "$dst/32" 2>/dev/null ;; esac
    done
  fi
}
cleanup() { uncut; disarm_deadman; }
trap 'cleanup' EXIT INT TERM

arm_deadman() {
  sudo -n systemctl stop tg-outage-deadman.service >/dev/null 2>&1
  sudo -n systemd-run --collect --unit=tg-outage-deadman /bin/sh -c \
    "sleep $DEAD_SECS; for n in $NETS; do /usr/bin/ip route del blackhole \$n; done; /usr/bin/logger -t tg-outage 'DEADMAN fired: ingest routes cleared'" >/dev/null 2>&1
}
disarm_deadman() {
  sudo -n systemctl stop tg-outage-deadman.service >/dev/null 2>&1
  sudo -n systemctl reset-failed tg-outage-deadman.service >/dev/null 2>&1
}

# prints: "<ok_all> <ok_live> <first_ok_epoch> <first_live_ok_epoch> <last_live_ok_epoch>" since epoch $1
trace_since() {
  python3 -c "
import json,sys
p,since=sys.argv[1],float(sys.argv[2])
oa=ol=fo=fl=ll=0
for L in open(p,errors='ignore'):
    L=L.strip()
    if not L: continue
    try: d=json.loads(L)
    except Exception: continue
    if d.get('ev')!='attempt': continue
    we=d.get('wall_epoch') or 0
    if we<since or not d.get('ok'): continue
    oa+=1
    if not fo: fo=we
    if d.get('source')=='live':
        ol+=1
        if not fl: fl=we
        if we>ll: ll=we
print(oa,ol,int(fo),int(fl),int(ll))
" "$TRACE" "$1" 2>/dev/null
}

sample() {   # $1 trial  $2 phase  $3 elapsed
  d=$(curl -s -m 4 http://127.0.0.1:8080/data 2>/dev/null)
  bc=$(jget "$d" buffer_count); no=$(jget "$d" network_online); ds=$(jget "$d" driver_state)
  t=$(vcgencmd measure_temp 2>/dev/null | tr -dc '0-9.')
  th=$(vcgencmd get_throttled 2>/dev/null | awk -F= '{print $2}')
  echo "$1,$2,$(date +%s),$(date -Is),$3,${bc:-},${no:-},${ds:-},${t:-},${th:-},$(bh_count)" >> "$POLL"
}

: > "$POLL"
echo "trial,phase,epoch,iso,elapsed_s,buffer_count,network_online,driver_state,temp_c,throttled,bh_routes" > "$POLL"

{
  echo "=== TransitGuard connection-loss & recovery trials v2 - $(date -Is) ==="
  echo "app HEAD            : $(cd $APP && git rev-parse HEAD 2>/dev/null)"
  echo "config sha256(env)  : $(sudo -n sha256sum /etc/transitguard.env 2>/dev/null | awk '{print $1}')"
  echo "trace file          : $TRACE  (bytes at start: $(stat -c %s "$TRACE" 2>/dev/null))"
  echo "ingest host         : $HOST"
  echo "host resolves to    : $(resolved_ips | tr '\n' ' ')"
  echo "blackhole ranges    : $NETS (+ resolved /32s, re-resolved during cut)"
  echo "method              : ingest-scoped blackhole; LAN/SSH/dashboard untouched"
  echo "                      LoRa = point-to-point UART radio, no internet path, NOT tested"
  echo "timing              : cut=${CUT_SECS}s  observe<=${OBS_MAX}s  poll=${POLL_SECS}s  deadman=${DEAD_SECS}s"
} | tee "$SUM"

cut >/dev/null 2>&1; uncut

for T in 1 2 3; do
  RESTORE_EPOCH=0
  echo "" | tee -a "$SUM"
  sample "$T" before 0
  before=$(awk -F, -v t="$T" '$1==t && $2=="before"{v=$6} END{print v}' "$POLL")
  echo "TRIAL $T  pending before outage = ${before:-?}   blackhole_routes=$(bh_count)" | tee -a "$SUM"

  arm_deadman
  logger -t tg-outage "TRIAL $T CUT"
  cut
  c=$(bh_count); p=$(reach)
  echo "TRIAL $T  CUT      $(date -Is)  probe_http=$p  blackhole_routes=$c  (want 000 and >=1)" | tee -a "$SUM"
  [ "$p" = 000 ] && [ "$c" -ge 1 ] && echo "TRIAL $T  cut verified" | tee -a "$SUM" || echo "TRIAL $T  !! CUT NOT EFFECTIVE" | tee -a "$SUM"

  # ---- outage window (absolute deadline), with leak checks ----
  t_end=$(( $(date +%s) + CUT_SECS ))
  e=0; k=0
  while [ "$(date +%s)" -lt "$t_end" ]; do
    sample "$T" during "$e"
    k=$((k+1))
    if [ $((k % 6)) -eq 0 ]; then
      add_resolved                       # re-add /32s if DNS rotated
      lp=$(reach)
      [ "$lp" = 000 ] || echo "$(date -Is) TRIAL $T CUT-LEAK probe_http=$lp" >> "$LEAK"
    fi
    sleep "$POLL_SECS"; e=$((e+POLL_SECS))
  done

  uncut
  RESTORE_EPOCH=$(date +%s)
  logger -t tg-outage "TRIAL $T RESTORED"
  p=$(reach); c=$(bh_count)
  echo "TRIAL $T  RESTORE  $(date -Is)  probe_http=$p  blackhole_routes=$c  (want non-000 and 0)" | tee -a "$SUM"
  [ "$p" != 000 ] && [ "$c" = 0 ] && echo "TRIAL $T  restore verified (block lifted)" | tee -a "$SUM" || echo "TRIAL $T  !! RESTORE INCOMPLETE" | tee -a "$SUM"

  peak=$(awk -F, -v t="$T" '$1==t && $2=="during"{if ($6+0>m) m=$6+0} END{print m+0}' "$POLL")
  atrest=$(awk -F, -v t="$T" '$1==t && $2=="during"{v=$6} END{print v}' "$POLL")
  echo "TRIAL $T  buffered during outage: peak=$peak  at-restore=$atrest" | tee -a "$SUM"

  # ---- recovery window: stop when queue==0 AND a fresh live 201 lands ----
  clear_at=""; first_ok_at=""; first_live_at=""; verdict="NOT CONFIRMED"
  t_end=$(( $(date +%s) + OBS_MAX ))
  e=0
  while [ "$(date +%s)" -lt "$t_end" ]; do
    sample "$T" after "$e"
    bc=$(curl -s -m 4 http://127.0.0.1:8080/data 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin).get('buffer_count'))" 2>/dev/null)
    st=$(trace_since "$RESTORE_EPOCH"); ok_all=$(echo "$st" | awk '{print $1}'); ok_live=$(echo "$st" | awk '{print $2}')
    fok=$(echo "$st" | awk '{print $3}'); flive=$(echo "$st" | awk '{print $4}'); llive=$(echo "$st" | awk '{print $5}')
    [ -z "$first_ok_at" ] && [ "${fok:-0}" -gt 0 ] 2>/dev/null && first_ok_at=$((fok-RESTORE_EPOCH))
    [ -z "$first_live_at" ] && [ "${flive:-0}" -gt 0 ] 2>/dev/null && first_live_at=$((flive-RESTORE_EPOCH))
    if [ -z "$clear_at" ] && [ "${bc:-x}" = 0 ] 2>/dev/null; then clear_at=$(( $(date +%s) - RESTORE_EPOCH )); fi
    if [ -n "$clear_at" ] && [ "${llive:-0}" -gt 0 ] 2>/dev/null && [ "$llive" -gt $((RESTORE_EPOCH+clear_at)) ] 2>/dev/null; then
      verdict="CONFIRMED"
      echo "TRIAL $T  early stop at t+${e}s (queue empty AND live 201 after clear)" | tee -a "$SUM"
      break
    fi
    sleep "$POLL_SECS"; e=$((e+POLL_SECS))
  done

  after=$(awk -F, -v t="$T" '$1==t && $2=="after"{v=$6} END{print v}' "$POLL")
  st=$(trace_since "$RESTORE_EPOCH"); ok_all=$(echo "$st" | awk '{print $1}'); ok_live=$(echo "$st" | awk '{print $2}')
  echo "TRIAL $T  recovery: first 201 at t+${first_ok_at:-none}s | first LIVE 201 at t+${first_live_at:-none}s | queue first empty at t+${clear_at:-never}s | remaining=${after:-?}" | tee -a "$SUM"
  echo "TRIAL $T  post-restore 201s: total=$ok_all live=$ok_live" | tee -a "$SUM"
  echo "TRIAL $T  RECOVERY $verdict" | tee -a "$SUM"
  disarm_deadman
done

uncut
disarm_deadman
echo "" | tee -a "$SUM"
echo "leak checks (empty = no cut leak): $(wc -l < "$LEAK" 2>/dev/null) line(s)" | tee -a "$SUM"
echo "=== run complete $(date -Is)  blackhole_routes=$(bh_count) ===" | tee -a "$SUM"
touch "$DONE"
