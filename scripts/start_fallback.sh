#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# start_fallback.sh – Push standby.mp4 loop to RTMP
#
# Usage:
#   ./start_fallback.sh channel1
#   RTMP_URL=rtmp://10.0.0.1/live/channel1 ./start_fallback.sh
#
# The script reads channels.json to get per-channel settings.
# Systemd runs this via stream-fallback@channel1.service.
# ─────────────────────────────────────────────────────────────

set -euo pipefail

CHANNEL="${1:-channel1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/config/channels.json"

# ── Parse channels.json for the requested channel ────────────
channel_json() {
    python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
chs = [c for c in cfg['channels'] if c['channel_name'] == '$CHANNEL']
if not chs:
    sys.exit('Channel $CHANNEL not found in channels.json')
c = chs[0]
print(c.get('rtmp_url',      'rtmp://127.0.0.1:1935/live/$CHANNEL'))
print(c.get('standby_file',  'standby/standby.mp4'))
print(c.get('bitrate',       3000))
print(c.get('audio_bitrate', 128))
print(c.get('fps',           30))
print(c.get('preset',        'veryfast'))
"
}

read -r RTMP_URL STANDBY_REL BITRATE AUDIO_BR FPS PRESET < <(channel_json)

STANDBY="$PROJECT_DIR/$STANDBY_REL"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG_FILE="$LOG_DIR/${CHANNEL}_fallback.log"
GOP=$((FPS * 2))

mkdir -p "$LOG_DIR"

if [[ ! -f "$STANDBY" ]]; then
    echo "ERROR: standby file not found: $STANDBY" >&2
    exit 1
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting fallback loop → $RTMP_URL" | tee -a "$LOG_FILE"
echo "  standby: $STANDBY"
echo "  bitrate: ${BITRATE}k  fps: $FPS  preset: $PRESET"

# ── Compliance check ─────────────────────────────────────────
# This script ONLY pushes a local standby file.
# Do NOT point RTMP_URL at a DRM-protected or unauthorized ingest source.

# ── FFmpeg fallback loop ──────────────────────────────────────
exec ffmpeg \
    -hide_banner \
    -loglevel warning \
    -re \
    -stream_loop -1 \
    -i "$STANDBY" \
    -c:v libx264 \
    -preset "$PRESET" \
    -tune zerolatency \
    -b:v "${BITRATE}k" \
    -maxrate "${BITRATE}k" \
    -bufsize "$((BITRATE * 2))k" \
    -r "$FPS" \
    -g "$GOP" \
    -c:a aac \
    -b:a "${AUDIO_BR}k" \
    -ar 44100 \
    -f flv \
    -reconnect 1 \
    -reconnect_streamed 1 \
    -reconnect_delay_max 5 \
    "$RTMP_URL" \
    2>&1 | tee -a "$LOG_FILE"
