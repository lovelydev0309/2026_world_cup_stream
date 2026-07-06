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
# Grace period after a (re)start before RPA will STALE-kill a script. A run_channel.sh
# whose source is fully dead (emits zero live segments) needs ~10-15s to exhaust its
# retries and fall to its OWN standby slate. The old code killed it every CHECK_INTERVAL,
# and each kill RESET its fail counter — trapping it in a kill-loop that never reached
# standby, so the channel went fully DARK instead of showing "please stand by". Only
# STALE-kill a script that has been alive longer than this and is STILL not producing
# (genuinely stuck, e.g. a hung ffmpeg the in-script watchdog somehow missed).
STALE_GRACE_SECS=90

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
    # Match BOTH mpegts (.ts) and fmp4/CMAF (.m4s) segments — a channel with
    # segment_type=fmp4 emits .m4s, so a .ts-only check would declare it dead and
    # restart-loop the producer every cycle.
    latest=$(ls -t "$hls_dir"/*.ts "$hls_dir"/*.m4s 2>/dev/null | head -1)
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

# Age (seconds) of the oldest run_channel.sh process for a channel, or 0 if none.
# Used to give a freshly (re)started script a grace period before a STALE-kill.
script_age() {
    local channel="$1" pid
    pid=$(pgrep -f "run_channel.sh $channel" | head -1)
    [ -z "$pid" ] && { echo 0; return; }
    ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' '
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
            # Only kill a STALE-but-ALIVE script once it has had STALE_GRACE_SECS to
            # self-recover to its standby slate (see note by STALE_GRACE_SECS). Killing a
            # freshly (re)started script resets its retry counter and traps a dead-source
            # channel in a kill-loop that never reaches standby → a dark channel.
            sage=$(script_age "$channel")
            if [ "${sage:-0}" -ge "$STALE_GRACE_SECS" ]; then
                log "STALE: $channel no fresh segments for >${STALE_GRACE_SECS}s (age ${sage}s) – killing and restarting"
                pkill -f "run_channel.sh $channel" 2>/dev/null || true
                sleep 2
                start_channel "$channel"
            fi
        fi
    done
    sleep "$CHECK_INTERVAL"
done
