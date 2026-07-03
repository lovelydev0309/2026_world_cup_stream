#!/usr/bin/env bash
# quality_probe.sh — periodic DEEP quality check per channel (complements the
# real-time stall-monitor). For each enabled channel it decodes ~6s of the live
# output and verifies: video is actually decoding (frame count), audio is present
# (not silent), resolution is right, and there are no decode errors. Logs one
# line per channel: OK or FLAG <what's wrong>. Run every ~10 min via cron.
# Reads the OUTPUT HLS on localhost (no IPTV connection cost). Log: logs/quality_probe.log
set -uo pipefail

PROJECT_DIR="/opt/streaming-stack"
CONFIG="$PROJECT_DIR/config/channels.json"
LOG="$PROJECT_DIR/logs/quality_probe.log"
BASE="http://127.0.0.1:8080/hls"
PROBE_SECS=6
SILENCE_DB=-55        # mean_volume below this ≈ silent audio
MIN_FRAMES=60         # expect ~180 for 6s@30fps; well below = frozen/broken video

ts(){ date -u '+%Y-%m-%dT%H:%M:%SZ'; }
log(){ echo "[$(ts)] $*" >> "$LOG"; }
channels(){ python3 -c "import json;[print(c['channel_name']) for c in json.load(open('$CONFIG'))['channels'] if c.get('enabled',True)]" 2>/dev/null; }
dispname(){ python3 -c "import json,sys;m={c['channel_name']:c.get('display_name',c['channel_name']) for c in json.load(open('$CONFIG'))['channels']};print(m.get(sys.argv[1],sys.argv[1]))" "$1" 2>/dev/null; }

flagged=0; total=0
for ch in $(channels); do
  total=$((total+1))
  url="$BASE/$ch/index.m3u8"
  res=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=s=x:p=0 "$url" 2>/dev/null)
  dec=$(ffmpeg -hide_banner -i "$url" -t "$PROBE_SECS" -af volumedetect -f null - 2>&1)
  frames=$(echo "$dec" | grep -oE 'frame= *[0-9]+' | tail -1 | grep -oE '[0-9]+$')
  mean=$(echo "$dec" | grep -oE 'mean_volume: *\-?[0-9.]+' | grep -oE '\-?[0-9.]+$' | head -1)
  errs=$(echo "$dec" | grep -icE 'error|corrupt|concealing|invalid data|non-monoton')

  flags=""
  [ -z "$res" ] && flags="$flags NO-VIDEO-STREAM"
  { [ -z "$frames" ] || [ "${frames:-0}" -lt "$MIN_FRAMES" ]; } && flags="$flags LOW-VIDEO(${frames:-0}frames)"
  if [ -z "$mean" ]; then flags="$flags NO-AUDIO"
  elif awk "BEGIN{exit !($mean < $SILENCE_DB)}"; then flags="$flags SILENT(${mean}dB)"; fi
  [ "${errs:-0}" -gt 5 ] && flags="$flags DECODE-ERRORS(${errs})"

  if [ -z "$flags" ]; then
    log "OK   $ch  res=${res} frames=${frames} audio=${mean}dB"
  else
    log "FLAG $ch $flags  res=${res} frames=${frames} audio=${mean}dB"
    flagged=$((flagged+1))
    "$PROJECT_DIR/scripts/send_alert.sh" "QUALITY issue on $ch - $(dispname "$ch") -$flags" \
      "Quality probe flagged $ch at $(ts):$flags (res=${res} frames=${frames} audio=${mean}dB). Check this channel's stream." >/dev/null 2>&1 &
  fi
done
log "RUN done: $((total-flagged))/${total} channels OK, ${flagged} flagged"
