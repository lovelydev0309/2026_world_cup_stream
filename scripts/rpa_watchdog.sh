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
STARTS_PER_CYCLE_MAX=15   # cold-start ramp — avoid all encoders on one tick
START_STAGGER_SECS=0.4   # pause between spawns to spread CPU/network load
# Grace period after a (re)start before RPA will STALE-kill a script. A run_channel.sh
# whose source is fully dead (emits zero live segments) needs ~10-15s to exhaust its
# retries and fall to its OWN standby slate. The old code killed it every CHECK_INTERVAL,
# and each kill RESET its fail counter — trapping it in a kill-loop that never reached
# standby, so the channel went fully DARK instead of showing "please stand by". Only
# STALE-kill a script that has been alive longer than this and is STILL not producing
# (genuinely stuck, e.g. a hung ffmpeg the in-script watchdog somehow missed).
STALE_GRACE_SECS=90

mkdir -p "$LOG_DIR"

# Only one RPA watchdog — a second instance doubles every channel encoder.
RPA_LOCK="/tmp/stream_rpa_watchdog.lock"
exec 8>"$RPA_LOCK"
if ! flock -n 8; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [RPA] Already running. Exiting." | tee -a "$LOG"
    exit 0
fi

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [RPA] $*"
    echo "$msg" | tee -a "$LOG"
}

# Get all enabled channel names from config.
# A 'quarantined' channel is one whose PROVIDER stopped broadcasting: quarantine_manager.py
# hides it from the client lineup and we must not produce it either. Skipping it here frees
# its provider connection slot and its transcode CPU — the exact pressure that makes the
# healthy channels flap — and stops it burning both on a feed that emits nothing.
get_channels() {
    python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for ch in cfg['channels']:
    if ch.get('enabled', True) and not ch.get('quarantined'):
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

# PIDs whose cmdline is exactly run_channel.sh <channel> (pgrep -f "channel1" falsely matches channel10-19).
match_channel_pids() {
    local channel="$1" pid cmd
    for pid in $(pgrep -f 'run_channel\.sh' 2>/dev/null); do
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        if echo "$cmd" | grep -qE "run_channel\.sh ${channel}( |$)"; then
            echo "$pid"
        fi
    done
}

# Match run_channel.sh for exactly this channel (not channel1 → channel10-19).
count_scripts() {
    local channel="$1" lock="/tmp/stream_lock_${channel}" n=0 pid target
    target=$(readlink -f "$lock" 2>/dev/null || echo "$lock")
    for pid in $(match_channel_pids "$channel"); do
        # Parent bash forks a worker child with the same cmdline; only the flock holder is real.
        if [ -r "/proc/$pid/fd/9" ] && [ "$(readlink -f "/proc/$pid/fd/9" 2>/dev/null)" = "$target" ]; then
            n=$((n + 1))
        fi
    done
    echo "$n"
}

# Check if run_channel.sh is running for a channel.
# Match the live process by name rather than a PID file: a stale or missing
# pidfile previously made the RPA's DEAD branch spawn a SECOND run_channel.sh
# next to a still-running one → two producers writing the same HLS dir → a
# corrupted/flapping manifest that freezes the player.
is_script_running() {
    local channel="$1" n
    n=$(count_scripts "$channel")
    [ "${n:-0}" -gt 0 ]
}

# Kill every run_channel.sh for this channel and wait until the flock is free.
# Never use pkill -f "run_channel.sh $channel" — channel1 is a substring of channel10-19.
stop_channel() {
    local channel="$1" i n pid
    for i in $(seq 1 15); do
        for pid in $(match_channel_pids "$channel"); do
            kill "$pid" 2>/dev/null || true
        done
        n=$(count_scripts "$channel")
        [ "${n:-0}" -eq 0 ] && break
        sleep 1
    done
    if [ "$(count_scripts "$channel")" -gt 0 ]; then
        for pid in $(match_channel_pids "$channel"); do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
    fi
    # Reap any forked worker still running without the flock
    for pid in $(match_channel_pids "$channel"); do
        kill -9 "$pid" 2>/dev/null || true
    done
}

# Age (seconds) of the oldest run_channel.sh process for a channel, or 0 if none.
# Used to give a freshly (re)started script a grace period before a STALE-kill.
script_age() {
    local channel="$1" lock="/tmp/stream_lock_${channel}" pid target
    target=$(readlink -f "$lock" 2>/dev/null || echo "$lock")
    for pid in $(match_channel_pids "$channel"); do
        if [ -r "/proc/$pid/fd/9" ] && [ "$(readlink -f "/proc/$pid/fd/9" 2>/dev/null)" = "$target" ]; then
            ps -o etimes= -p "$pid" 2>/dev/null | tr -d ' '
            return
        fi
    done
    echo 0
}

# Start a channel (never stack duplicates — two writers corrupt the HLS manifest).
start_channel() {
    local channel="$1" n
    n=$(count_scripts "$channel")
    if [ "${n:-0}" -gt 0 ]; then
        if [ "$n" -eq 1 ]; then
            return 0
        fi
        log "DUPLICATE: $channel has $n scripts – stopping extras"
        stop_channel "$channel"
    fi
    # Ensure HLS dir exists with correct permissions
    docker exec nginx-rtmp sh -c \
        "mkdir -p /var/www/hls/$channel && chmod 1777 /var/www/hls/$channel" 2>/dev/null || true
    setsid bash "$SCRIPT_DIR/run_channel.sh" "$channel" \
        </dev/null >>"$LOG_DIR/${channel}_stdout.log" 2>&1 &
    log "Started $channel (PID $!)"
    sleep "$START_STAGGER_SECS"
}

log "=== RPA Watchdog started ==="
log "Monitoring channels: $(get_channels | tr '\n' ' ')"

while true; do
    starts_this_cycle=0
    for channel in $(get_channels); do
        n=$(count_scripts "$channel")
        if [ "${n:-0}" -gt 1 ]; then
            log "DUPLICATE: $channel has $n scripts – stopping extras"
            stop_channel "$channel"
            start_channel "$channel"
        elif ! is_script_running "$channel"; then
            if [ "$starts_this_cycle" -ge "$STARTS_PER_CYCLE_MAX" ]; then
                continue
            fi
            log "DEAD: $channel script not running – restarting"
            start_channel "$channel"
            starts_this_cycle=$((starts_this_cycle + 1))
        elif ! is_stream_alive "$channel"; then
            # Only kill a STALE-but-ALIVE script once it has had STALE_GRACE_SECS to
            # self-recover to its standby slate (see note by STALE_GRACE_SECS). Killing a
            # freshly (re)started script resets its retry counter and traps a dead-source
            # channel in a kill-loop that never reaches standby → a dark channel.
            sage=$(script_age "$channel")
            if [ "${sage:-0}" -ge "$STALE_GRACE_SECS" ]; then
                log "STALE: $channel no fresh segments for >${STALE_GRACE_SECS}s (age ${sage}s) – killing and restarting"
                stop_channel "$channel"
                start_channel "$channel"
            fi
        fi
    done
    sleep "$CHECK_INTERVAL"
done
