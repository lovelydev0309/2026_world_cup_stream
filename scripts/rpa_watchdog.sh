#!/usr/bin/env bash
# rpa_watchdog.sh – Master RPA: monitors all channels, auto-restarts dead streams.
# Runs as a systemd service (streaming-rpa.service).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/config/channels.json"
LOG_DIR="$PROJECT_DIR/logs"
LOG="$LOG_DIR/rpa_watchdog.log"
CHECK_INTERVAL=10   # seconds between checks
STALE_THRESHOLD=60  # seconds before declaring a stream dead (allow codec probe + ffmpeg startup)

mkdir -p "$LOG_DIR"

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [RPA] $*"
    echo "$msg" | tee -a "$LOG"
}

# Get all enabled channel names from config
get_channels() {
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for ch in cfg['channels']:
    if ch.get('enabled', True):
        print(ch['channel_name'])
"
}

# Check if a channel stream is alive (segment written recently)
is_stream_alive() {
    local channel="$1"
    local hls_dir="$PROJECT_DIR/hls/$channel"
    local latest
    latest=$(ls -t "$hls_dir"/*.ts 2>/dev/null | head -1)
    [ -z "$latest" ] && return 1
    local age=$(( $(date +%s) - $(stat -c %Y "$latest" 2>/dev/null || echo 0) ))
    [ "$age" -lt "$STALE_THRESHOLD" ]
}

# Check if run_channel.sh is running for a channel.
# Match the live process by name rather than a PID file: a stale or missing
# pidfile previously made the RPA's DEAD branch spawn a SECOND run_channel.sh
# next to a still-running one → two producers writing the same HLS dir → a
# corrupted/flapping manifest that freezes the player.
is_script_running() {
    local channel="$1"
    pgrep -f "run_channel.sh $channel" >/dev/null 2>&1
}

# Start a channel
start_channel() {
    local channel="$1"
    # Ensure HLS dir exists with correct permissions
    docker exec nginx-rtmp sh -c \
        "mkdir -p /var/www/hls/$channel && chmod 1777 /var/www/hls/$channel" 2>/dev/null || true
    setsid bash "$SCRIPT_DIR/run_channel.sh" "$channel" \
        </dev/null >>"$LOG_DIR/${channel}_stdout.log" 2>&1 &
    log "Started $channel (PID $!)"
}

log "=== RPA Watchdog started ==="
log "Monitoring channels: $(get_channels | tr '\n' ' ')"

while true; do
    for channel in $(get_channels); do
        if ! is_script_running "$channel"; then
            log "DEAD: $channel script not running – restarting"
            start_channel "$channel"
        elif ! is_stream_alive "$channel"; then
            log "STALE: $channel has no fresh segments – killing and restarting"
            pkill -f "run_channel.sh $channel" 2>/dev/null || true
            sleep 2
            start_channel "$channel"
        fi
    done
    sleep "$CHECK_INTERVAL"
done
