#!/usr/bin/env bash
# quality_probe.sh — periodic DEEP quality check per channel (complements the
# real-time stall-monitor). For each enabled channel it decodes ~6s of the live
# output and verifies: video is actually decoding (frame count), audio is present
# (not silent), resolution is right, and there are no decode errors. A flagged
# channel is RE-PROBED to confirm the issue is persistent (a transient live-edge
# blip clears on recheck) — only a confirmed problem alerts. Run every ~10 min via
# cron. Reads the OUTPUT HLS on localhost (no IPTV connection cost).
set -uo pipefail

PROJECT_DIR="/opt/streaming-stack"
CONFIG="$PROJECT_DIR/config/channels.json"
LOG="$PROJECT_DIR/logs/quality_probe.log"
BASE="http://127.0.0.1:8080/hls"
PROBE_SECS=6
SILENCE_DB=-55        # mean_volume below this ≈ silent audio
MIN_FRAMES=60         # expect ~180 for 6s@30fps; well below = frozen/broken video
RECHECK_WAIT=3        # pause before the confirmation re-probe

ts(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }
channels(){ python3 -c "import json;[print(c['channel_name']) for c in json.load(open('$CONFIG'))['channels'] if c.get('enabled',True)]" 2>/dev/null; }
dispname(){ python3 -c "import json,sys;m={c['channel_name']:c.get('display_name',c['channel_name']) for c in json.load(open('$CONFIG'))['channels']};print(m.get(sys.argv[1],sys.argv[1]))" "$1" 2>/dev/null; }

# probe_channel <ch> -> sets globals R_FLAGS R_RES R_FRAMES R_MEAN
probe_channel(){
  local ch="$1" url="$BASE/$1/index.m3u8" dec errs
  R_RES=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$url" 2>/dev/null)
  dec=$(ffmpeg -hide_banner -i "$url" -t "$PROBE_SECS" -af volumedetect -f null - 2>&1)
  R_FRAMES=$(echo "$dec" | grep -oE 'frame= *[0-9]+' | tail -1 | grep -oE '[0-9]+$')
  R_MEAN=$(echo "$dec" | grep -oE 'mean_volume: *\-?[0-9.]+' | grep -oE '\-?[0-9.]+$' | head -1)
  errs=$(echo "$dec" | grep -icE 'error|corrupt|concealing|invalid data|non-monoton')
  R_FLAGS=""
  [ -z "$R_RES" ] && R_FLAGS="$R_FLAGS NO-VIDEO-STREAM"
  { [ -z "$R_FRAMES" ] || [ "${R_FRAMES:-0}" -lt "$MIN_FRAMES" ]; } && R_FLAGS="$R_FLAGS LOW-VIDEO(${R_FRAMES:-0}frames)"
  if [ -z "$R_MEAN" ]; then R_FLAGS="$R_FLAGS NO-AUDIO"
  elif awk "BEGIN{exit !($R_MEAN < $SILENCE_DB)}"; then R_FLAGS="$R_FLAGS SILENT(${R_MEAN}dB)"; fi
  [ "${errs:-0}" -gt 5 ] && R_FLAGS="$R_FLAGS DECODE-ERRORS(${errs})"
}

flagged=0; total=0
for ch in $(channels); do
  total=$((total+1))
  probe_channel "$ch"
  if [ -z "$R_FLAGS" ]; then
    log "OK   $ch  res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN}dB"
    continue
  fi
  # confirm: re-probe once. A transient live-edge/IO blip clears; a real fault stays.
  first="$R_FLAGS"
  sleep "$RECHECK_WAIT"
  probe_channel "$ch"
  if [ -z "$R_FLAGS" ]; then
    log "TRANSIENT $ch  first='${first# }' cleared on recheck (res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN}dB) — no alert"
  else
    log "FLAG $ch$R_FLAGS  res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN}dB  (confirmed on recheck)"
    flagged=$((flagged+1))
    "$PROJECT_DIR/scripts/send_alert.sh" "QUALITY issue on $ch - $(dispname "$ch") -$R_FLAGS" \
      "Quality probe flagged $ch ($(dispname "$ch")) at $(ts):$R_FLAGS — confirmed on a re-check (res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN}dB). This is a persistent issue; check the channel's stream." >/dev/null 2>&1 &
  fi
done
log "RUN done: $((total-flagged))/${total} channels OK, ${flagged} flagged"
