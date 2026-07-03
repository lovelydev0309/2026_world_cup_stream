#!/usr/bin/env bash
# quality_probe.sh — periodic DEEP quality check per channel (complements the
# real-time stall-monitor). Decodes ~6s of each channel's live OUTPUT and checks:
# video actually decoding (frame count), audio present (stream + not silent),
# resolution correct, no decode errors. Guards against false positives:
#   - SKIP channels that are mid-recovery (stale segment) — the stall-monitor owns them.
#   - NO-AUDIO only if the audio STREAM is absent (empty volumedetect on a present
#     stream is a measurement artifact, not a fault).
#   - Any flag is RE-CHECKED 15s later; only a persistent fault alerts.
# Reads localhost HLS (no IPTV connection cost). Cron ~every 10 min.
set -uo pipefail

PROJECT_DIR="/opt/streaming-stack"
CONFIG="$PROJECT_DIR/config/channels.json"
HLS_DIR="$PROJECT_DIR/hls"
LOG="$PROJECT_DIR/logs/quality_probe.log"
BASE="http://127.0.0.1:8080/hls"
PROBE_SECS=6
SILENCE_DB=-55        # measured mean_volume below this = genuinely silent
MIN_FRAMES=60         # expect ~180 for 6s@30fps; well below = frozen video
FRESH_MAX=10          # newest segment older than this = mid-recovery -> skip (stall-monitor's job)
RECHECK_WAIT=15       # confirmation delay — long enough for a failover to finish

ts(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }
channels(){ python3 -c "import json;[print(c['channel_name']) for c in json.load(open('$CONFIG'))['channels'] if c.get('enabled',True)]" 2>/dev/null; }
dispname(){ python3 -c "import json,sys;m={c['channel_name']:c.get('display_name',c['channel_name']) for c in json.load(open('$CONFIG'))['channels']};print(m.get(sys.argv[1],sys.argv[1]))" "$1" 2>/dev/null; }
seg_age(){ local f; f=$(ls -t "$HLS_DIR/$1"/*.ts 2>/dev/null | head -1); [ -n "$f" ] && echo $(( $(date +%s) - $(stat -c %Y "$f") )) || echo 999; }

# probe_channel <ch> -> sets R_FLAGS R_RES R_FRAMES R_MEAN
probe_channel(){
  local url="$BASE/$1/index.m3u8" dec errs has_audio
  R_RES=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$url" 2>/dev/null)
  has_audio=$(ffprobe -v error -select_streams a -show_entries stream=index -of csv=p=0 "$url" 2>/dev/null | grep -c .)
  dec=$(ffmpeg -hide_banner -i "$url" -t "$PROBE_SECS" -map 0:a:0 -af volumedetect -f null - 2>&1)
  R_FRAMES=$(echo "$dec" | grep -oE 'frame= *[0-9]+' | tail -1 | grep -oE '[0-9]+$')
  R_MEAN=$(echo "$dec" | grep -oE 'mean_volume: *\-?[0-9.]+' | grep -oE '\-?[0-9.]+$' | head -1)
  errs=$(echo "$dec" | grep -icE 'error|corrupt|concealing|invalid data|non-monoton')
  R_FLAGS=""
  [ -z "$R_RES" ] && R_FLAGS="$R_FLAGS NO-VIDEO-STREAM"
  { [ -z "$R_FRAMES" ] || [ "${R_FRAMES:-0}" -lt "$MIN_FRAMES" ]; } && R_FLAGS="$R_FLAGS LOW-VIDEO(${R_FRAMES:-0}frames)"
  if [ "${has_audio:-0}" -eq 0 ]; then R_FLAGS="$R_FLAGS NO-AUDIO-STREAM"
  elif [ -n "$R_MEAN" ] && awk "BEGIN{exit !($R_MEAN < $SILENCE_DB)}"; then R_FLAGS="$R_FLAGS SILENT(${R_MEAN}dB)"; fi
  [ "${errs:-0}" -gt 5 ] && R_FLAGS="$R_FLAGS DECODE-ERRORS(${errs})"
}

flagged=0; total=0; skipped=0
for ch in $(channels); do
  total=$((total+1))
  if [ "$(seg_age "$ch")" -gt "$FRESH_MAX" ]; then
    log "SKIP $ch  mid-recovery (stale segment) — stall-monitor owns it"
    skipped=$((skipped+1)); continue
  fi
  probe_channel "$ch"
  if [ -z "$R_FLAGS" ]; then
    log "OK   $ch  res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN:-n/a}dB"
    continue
  fi
  first="$R_FLAGS"
  sleep "$RECHECK_WAIT"
  if [ "$(seg_age "$ch")" -gt "$FRESH_MAX" ]; then
    log "TRANSIENT $ch  first='${first# }' then went mid-recovery — no alert"; continue
  fi
  probe_channel "$ch"
  if [ -z "$R_FLAGS" ]; then
    log "TRANSIENT $ch  first='${first# }' cleared on recheck — no alert"
  else
    log "FLAG $ch$R_FLAGS  res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN:-n/a}dB  (confirmed)"
    flagged=$((flagged+1))
    "$PROJECT_DIR/scripts/send_alert.sh" "QUALITY issue on $ch - $(dispname "$ch") -$R_FLAGS" \
      "Quality probe flagged $ch ($(dispname "$ch")) at $(ts):$R_FLAGS — confirmed on a re-check 15s later (res=${R_RES} frames=${R_FRAMES} audio=${R_MEAN:-n/a}dB). Persistent issue; check the channel's stream." >/dev/null 2>&1 &
  fi
done
log "RUN done: $((total-flagged-skipped))/${total} OK, ${flagged} flagged, ${skipped} skipped(recovering)"
