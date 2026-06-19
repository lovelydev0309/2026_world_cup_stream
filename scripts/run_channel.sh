#!/usr/bin/env bash
# run_channel.sh – Pull IPTV source → HLS with stale-segment watchdog.
set -uo pipefail

CHANNEL="${1:-channel1}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/config/channels.json"
LOG_DIR="${LOG_DIR:-$PROJECT_DIR/logs}"
LOG="$LOG_DIR/${CHANNEL}.log"
PIDFILE="/tmp/stream_${CHANNEL}.pid"
FFMPEG_PID_FILE="/tmp/ffmpeg_${CHANNEL}.pid"

mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    local msg="[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"
    echo "$msg"
    echo "$msg" >> "$LOG" 2>/dev/null || true
}

# ── Exclusive singleton lock (flock) ─────────────────────────
# Only one instance per channel. If another is already running,
# exit immediately. flock -n = non-blocking (don't wait).
LOCKFILE="/tmp/stream_lock_${CHANNEL}"
exec 9>"$LOCKFILE"
if ! flock -n 9; then
    log "Already running (lock held). Exiting."
    exit 0
fi
echo $$ > "$PIDFILE"
WATCHDOG_PID=0
trap "flock -u 9; rm -f $PIDFILE $FFMPEG_PID_FILE $LOCKFILE; kill \$WATCHDOG_PID 2>/dev/null; pkill -P $$ 2>/dev/null; exit" INT TERM EXIT

# ── Read config ───────────────────────────────────────────────
read -r SOURCE_URL STANDBY_REL BITRATE AUDIO_BR FPS < <(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
chs = [c for c in cfg['channels'] if c['channel_name'] == '$CHANNEL']
if not chs: sys.exit('Channel not found')
c = chs[0]
print(c.get('source_url',''), c.get('standby_file','standby/standby.mp4'),
      c.get('bitrate',2500), c.get('audio_bitrate',128), c.get('fps',30))")

STANDBY="$PROJECT_DIR/$STANDBY_REL"
HLS_DIR="$PROJECT_DIR/hls/$CHANNEL"
GOP=$((FPS * 2))
STALE_KILL_SECS=30   # kill ffmpeg if no new segment for this many seconds

log "=== START $CHANNEL ==="
log "  source=$SOURCE_URL fps=$FPS gop=$GOP"

# ── Ensure HLS dir ─────────────────────────────────────────────
ensure_hls_dir() {
    if [ ! -d "$HLS_DIR" ] || [ ! -w "$HLS_DIR" ]; then
        log "  [WARN] HLS dir missing/unwritable – recreating"
        docker exec nginx-rtmp sh -c \
            "mkdir -p /var/www/hls/$CHANNEL && chmod 1777 /var/www/hls/$CHANNEL" 2>/dev/null \
        || { mkdir -p "$HLS_DIR" && chmod 1777 "$HLS_DIR"; }
    fi
}
ensure_hls_dir

# ── Stale-segment watchdog ────────────────────────────────────
# Started fresh inside push_live for each FFmpeg attempt so it has
# no memory of old segments from prior runs. Kills ffmpeg only if
# the newest .ts file has NOT CHANGED for STALE_KILL_SECS seconds.
stale_watchdog() {
    local ffpid="$1"
    local last_seg="" current_seg stale_count=0
    local threshold=$(( STALE_KILL_SECS / 5 ))  # 5-second check interval
    while kill -0 "$ffpid" 2>/dev/null; do
        sleep 5
        current_seg=$(ls -t "$HLS_DIR"/*.ts 2>/dev/null | head -1)
        if [ -z "$current_seg" ] || [ "$current_seg" = "$last_seg" ]; then
            stale_count=$((stale_count + 1))
        else
            stale_count=0
            last_seg="$current_seg"
        fi
        if [ $stale_count -ge $threshold ]; then
            log "  [WATCHDOG] No new segment for ${STALE_KILL_SECS}s – killing ffmpeg PID $ffpid"
            kill -9 "$ffpid" 2>/dev/null
            break
        fi
    done
}
WATCHDOG_PID=0

# ── Detect source codec (H.264 vs HEVC) ──────────────────────
# Caches result in /tmp/codec_${CHANNEL} so restarts don't re-probe.
# Falls back to cached value if probe times out (source temporarily down).
CODEC_CACHE="$PROJECT_DIR/cache/codec_${CHANNEL}"
mkdir -p "$PROJECT_DIR/cache" 2>/dev/null || true
detect_codec() {
    local result
    # 5s timeout (was 10s): on a flaky source this probe runs on every
    # reconnect, so keep it short — it succeeds in 1-2s when the source is up
    # and falls back to the cached codec otherwise.
    result=$(timeout 5 ffprobe -v quiet -hide_banner \
        -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
        -analyzeduration 2000000 -probesize 1000000 \
        -show_streams -select_streams v:0 \
        -print_format csv \
        "$SOURCE_URL" 2>/dev/null | awk -F',' 'NR==1{print $3}')
    if [ -n "$result" ] && [ "$result" != "unknown" ]; then
        echo "$result" > "$CODEC_CACHE"
        echo "$result"
    elif [ -f "$CODEC_CACHE" ]; then
        # Write log directly to file – NOT stdout, which is captured by $() callers
        printf '[%s]   codec probe failed – using cached: %s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(cat "$CODEC_CACHE")" >> "$LOG" 2>/dev/null || true
        cat "$CODEC_CACHE"
    else
        echo "unknown"
    fi
}

# ── Live push ─────────────────────────────────────────────────
# Only confirmed h264 uses stream copy (~2% CPU).
# Everything else (hevc, unknown, or any probe failure) re-encodes to H.264
# 540p – safe for all browsers and avoids broken stream copy on HEVC.
push_live() {
    ensure_hls_dir
    local codec
    codec=$(detect_codec)
    log "→ LIVE codec=${codec:-unknown}"

    if [ "$codec" = "h264" ]; then
        log "  H.264 source – stream copy (~2% CPU)"
        ffmpeg -hide_banner -loglevel warning \
            -fflags +igndts+discardcorrupt+genpts \
            -err_detect ignore_err \
            -use_wallclock_as_timestamps 1 \
            -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
            -headers "Referer: http://prosclan.fans/" \
            -reconnect 1 -reconnect_at_eof 1 \
            -reconnect_streamed 1 -reconnect_delay_max 5 \
            -timeout 10000000 \
            -i "$SOURCE_URL" \
            -c:v copy \
            -c:a copy \
            -f hls -hls_time 4 -hls_list_size 20 \
            -hls_flags delete_segments+append_list+independent_segments \
            -hls_segment_type mpegts \
            -hls_segment_filename "$HLS_DIR/%d.ts" \
            "$HLS_DIR/index.m3u8" \
            2>&1 &
    else
        log "  non-H.264 source (${codec:-unknown}) – re-encoding to H.264 540p"
        ffmpeg -hide_banner -loglevel warning \
            -fflags +igndts+discardcorrupt+genpts \
            -err_detect ignore_err \
            -use_wallclock_as_timestamps 1 \
            -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
            -headers "Referer: http://prosclan.fans/" \
            -reconnect 1 -reconnect_at_eof 1 \
            -reconnect_streamed 1 -reconnect_delay_max 5 \
            -timeout 10000000 \
            -i "$SOURCE_URL" \
            -vf "scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2" \
            -c:v libx264 -preset ultrafast -crf 28 \
            -r "$FPS" -g "$GOP" -keyint_min "$GOP" \
            -force_key_frames "expr:gte(t,n_forced*4)" \
            -c:a aac -b:a "${AUDIO_BR}k" -ar 44100 \
            -af aresample=async=1 \
            -f hls -hls_time 4 -hls_list_size 20 \
            -hls_flags delete_segments+append_list+independent_segments \
            -hls_segment_type mpegts \
            -hls_segment_filename "$HLS_DIR/%d.ts" \
            "$HLS_DIR/index.m3u8" \
            2>&1 &
    fi

    local FPID=$!
    echo $FPID > "$FFMPEG_PID_FILE"
    # Fresh watchdog per FFmpeg run — no stale state from old segments
    stale_watchdog "$FPID" &
    WATCHDOG_PID=$!
    wait $FPID
    kill $WATCHDOG_PID 2>/dev/null; wait $WATCHDOG_PID 2>/dev/null
    rm -f "$FFMPEG_PID_FILE"
    return $?
}

# ── Standby: 30s cycle of standby.mp4 ─────────────────────────
push_standby() {
    ensure_hls_dir
    log "→ STANDBY"
    ffmpeg -hide_banner -loglevel warning \
        -re -stream_loop -1 -i "$STANDBY" \
        -c:v libx264 -preset ultrafast -crf 26 \
        -r "$FPS" -g "$GOP" \
        -force_key_frames "expr:gte(t,n_forced*2)" \
        -c:a aac -b:a "${AUDIO_BR}k" -ar 44100 \
        -t 30 \
        -f hls -hls_time 2 -hls_list_size 20 \
        -hls_flags delete_segments+append_list+independent_segments \
        -hls_segment_type mpegts \
        -hls_segment_filename "$HLS_DIR/%d.ts" \
        "$HLS_DIR/index.m3u8" \
        2>&1 &
    local FPID=$!
    echo $FPID > "$FFMPEG_PID_FILE"
    stale_watchdog "$FPID" &
    WATCHDOG_PID=$!
    wait $FPID
    kill $WATCHDOG_PID 2>/dev/null; wait $WATCHDOG_PID 2>/dev/null
    rm -f "$FFMPEG_PID_FILE"
}

# ── Main loop ─────────────────────────────────────────────────
# Standby (the holding-pattern clip) should ONLY trigger when the source
# genuinely can't establish a stream — i.e. rapid back-to-back failures.
# A run that streamed fine for minutes and then hit a transient upstream
# drop (e.g. HTTP 509 from the IPTV provider) is NOT a crash-loop; it should
# reconnect immediately with no standby blackout.
LIVE_FAIL=0
MAX_FAILS=3
HEALTHY_RUN_SECS=45   # a run at least this long = healthy, resets the fail counter

while true; do
    if [[ -n "$SOURCE_URL" ]] && [[ $LIVE_FAIL -lt $MAX_FAILS ]]; then
        T_START=$(date +%s)
        push_live
        EXIT=$?
        T_RUN=$(( $(date +%s) - T_START ))
        if [[ $T_RUN -ge $HEALTHY_RUN_SECS ]]; then
            # Healthy stream hit a transient drop – reconnect now, no penalty.
            LIVE_FAIL=0
            log "Live exited (code=$EXIT) ran=${T_RUN}s – healthy, reconnecting immediately"
        else
            LIVE_FAIL=$((LIVE_FAIL + 1))
            log "Live exited (code=$EXIT) ran=${T_RUN}s fail=$LIVE_FAIL/$MAX_FAILS"
        fi
    else
        push_standby || true
        log "Standby ended – resetting, retrying live"
        LIVE_FAIL=0
    fi
    sleep 1
done
