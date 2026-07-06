#!/usr/bin/env bash
# stall_monitor.sh — continuous per-channel freeze recorder.
# Samples each enabled channel's newest HLS segment every INTERVAL seconds. When a
# channel produces no new segment for > STALL_SECS it logs a STALL (with the source
# edge it was on); on recovery it logs the max production gap and whether it was
# buffer-absorbed (< player's ~45s buffer = invisible to viewers) or VIEWER-VISIBLE.
# Emits an hourly SUMMARY. Runs as stall-monitor.service.  Log: logs/stall_monitor.log
set -uo pipefail

PROJECT_DIR="/opt/streaming-stack"
HLS_DIR="$PROJECT_DIR/hls"
CONFIG="$PROJECT_DIR/config/channels.json"
LOG="$PROJECT_DIR/logs/stall_monitor.log"
STALL_SECS=15          # no new segment for this long = a stall
INTERVAL=5             # sample cadence
BUFFER_SECS=60         # player live buffer (hls.js maxBufferLength=60); gaps under this are
                       # absorbed by the viewer's buffer → NOT a visible freeze. Was 45, which
                       # miscounted 45-58s flap gaps that the real 60s buffer rides out.
SUMMARY_EVERY=1800     # 30-min rollup + ONE digest email (was hourly, log-only)

mkdir -p "$PROJECT_DIR/logs"
ts(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }
channels(){ python3 -c "import json;[print(c['channel_name']) for c in json.load(open('$CONFIG'))['channels'] if c.get('enabled',True)]" 2>/dev/null; }
dispname(){ python3 -c "import json,sys;m={c['channel_name']:c.get('display_name',c['channel_name']) for c in json.load(open('$CONFIG'))['channels']};print(m.get(sys.argv[1],sys.argv[1]))" "$1" 2>/dev/null; }

declare -A since        # stall-start epoch per channel
declare -A maxage       # max segment age reached during the current stall
declare -A cnt          # stall count per channel since last summary
declare -A secs         # total stalled seconds since last summary
declare -A vis          # viewer-visible stall count since last summary
last_summary=$(date +%s)

log "=== stall-monitor started (stall>${STALL_SECS}s, sample ${INTERVAL}s, buffer ${BUFFER_SECS}s) ==="

while true; do
  now=$(date +%s)
  for ch in $(channels); do
    f=$(ls -t "$HLS_DIR/$ch"/*.ts "$HLS_DIR/$ch"/*.m4s 2>/dev/null | head -1)
    if [ -n "$f" ]; then age=$(( now - $(stat -c %Y "$f" 2>/dev/null || echo "$now") )); else age=999; fi
    if [ "$age" -gt "$STALL_SECS" ]; then
      if [ -z "${since[$ch]:-}" ]; then
        since[$ch]=$now; maxage[$ch]=$age
        src=$(ps -eo args 2>/dev/null | grep "[h]ls/$ch/" | grep -oE 'https?://[^/ ]+' | head -1)
        log "STALL   $ch  no new segment ${age}s  edge=${src:-unknown}"
      elif [ "$age" -gt "${maxage[$ch]}" ]; then
        maxage[$ch]=$age
      fi
    else
      if [ -n "${since[$ch]:-}" ]; then
        gap=${maxage[$ch]}
        if [ "$gap" -lt "$BUFFER_SECS" ]; then
          note="(buffer-absorbed, no viewer freeze)"
        else
          # Count for the periodic DIGEST instead of emailing on every freeze: a provider-wide
          # bad window flaps many channels and per-freeze mail floods the inbox (the "continuous
          # error email" problem). One digest per SUMMARY_EVERY covers them all.
          note="(VIEWER-VISIBLE FREEZE)"; vis[$ch]=$(( ${vis[$ch]:-0} + 1 ))
        fi
        log "RECOVER $ch  max-gap ${gap}s $note"
        cnt[$ch]=$(( ${cnt[$ch]:-0} + 1 )); secs[$ch]=$(( ${secs[$ch]:-0} + gap ))
        unset since[$ch]; unset maxage[$ch]
      fi
    fi
  done

  if [ $(( now - last_summary )) -ge "$SUMMARY_EVERY" ]; then
    line=""; any=0; digest=""; vis_any=0
    for ch in $(channels); do
      if [ -n "${cnt[$ch]:-}" ]; then
        line="$line  $ch=${cnt[$ch]}stall/${secs[$ch]:-0}s"
        if [ -n "${vis[$ch]:-}" ]; then
          line="$line(${vis[$ch]}visible)"
          digest="$digest  - $ch ($(dispname "$ch")): ${vis[$ch]} visible freeze(s)"$'\n'
          vis_any=1
        fi
        any=1
      fi
    done
    [ "$any" = 0 ] && line="  all channels 100% smooth — zero stalls"
    log "SUMMARY(30m):$line"
    # ONE digest email per window, and ONLY if there were viewer-visible freezes. Stable
    # subject so the send_alert cooldown can't be defeated by varying content.
    if [ "$vis_any" = 1 ]; then
      "$PROJECT_DIR/scripts/send_alert.sh" "Stream health digest - viewer-visible freezes" \
"In the last $(( SUMMARY_EVERY/60 )) min these channels had viewer-visible freezes (all auto-recovered to standby/live):
$digest
Recurring freezes across several channels = upstream provider (tvon247) feed degradation and/or connection-slot pressure (15 channels sharing 16 provider connection slots). Per-event detail in logs/stall_monitor.log." >/dev/null 2>&1 &
    fi
    unset cnt secs vis; declare -A cnt secs vis
    last_summary=$now
  fi
  sleep "$INTERVAL"
done
