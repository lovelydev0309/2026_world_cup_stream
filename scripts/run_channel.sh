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
# NOTE: do NOT rm $LOCKFILE in this trap. Deleting the lockfile path frees the
# name while another instance may still hold a flock on the old inode; the next
# instance then creates a FRESH inode at the same path and its `flock -n`
# succeeds → two run_channel.sh produce into the same HLS dir at once, both
# rewriting index.m3u8, which corrupts the playlist and freezes the player.
# The lockfile is a 0-byte marker — leave it on disk permanently so every
# instance flocks the SAME inode and duplicates are reliably rejected.
trap "flock -u 9; rm -f $PIDFILE $FFMPEG_PID_FILE; kill \$WATCHDOG_PID 2>/dev/null; pkill -P $$ 2>/dev/null; exit" INT TERM EXIT

# ── Read config ───────────────────────────────────────────────
# Scalar fields (standby/bitrate/fps) on one line …
read -r STANDBY_REL BITRATE AUDIO_BR FPS < <(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
chs = [c for c in cfg['channels'] if c['channel_name'] == '$CHANNEL']
if not chs: sys.exit('Channel not found')
c = chs[0]
print(c.get('standby_file','standby/standby.mp4'),
      c.get('bitrate',2500), c.get('audio_bitrate',128), c.get('fps',30))")

# … and the ordered source URL list (primary + optional backups) into an array.
# Prefers source_urls[] if present, else falls back to the single source_url.
mapfile -t SOURCE_URLS < <(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
c = [c for c in cfg['channels'] if c['channel_name'] == '$CHANNEL'][0]
urls = c.get('source_urls') or ([c['source_url']] if c.get('source_url') else [])
for u in urls:
    if u: print(u)")

NUM_URLS=${#SOURCE_URLS[@]}
SOURCE_URL="${SOURCE_URLS[0]:-}"   # active URL; rotated by the main loop on failover

STANDBY="$PROJECT_DIR/$STANDBY_REL"
HLS_DIR="$PROJECT_DIR/hls/$CHANNEL"
GOP=$((FPS * 2))
STALE_KILL_SECS=45   # kill ffmpeg only after this many seconds of ZERO write progress

log "=== START $CHANNEL ==="
log "  source=$SOURCE_URL fps=$FPS gop=$GOP (${NUM_URLS} source URL(s))"

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
# no memory of old segments from prior runs.
#
# IMPORTANT: it must distinguish a genuinely FROZEN ffmpeg from one that is
# healthily writing a LONG segment. In stream-copy mode ffmpeg can only cut on
# the source's keyframes, so a sparse-keyframe / briefly-stalling IPTV source
# can legitimately produce a single segment that takes 30-60s to finish. The
# old check ("did a NEW .ts filename appear?") false-killed those healthy runs,
# forcing a restart → #EXT-X-DISCONTINUITY → orphaned segment 404 → the player
# jumping to the live edge. So we treat "progress" as EITHER a new segment OR
# the current newest segment still GROWING in bytes; only true zero-progress
# (no new file AND no byte growth) counts toward the kill.
stale_watchdog() {
    local ffpid="$1"
    local last_seg="" last_size=0 current_seg current_size stale_count=0
    local interval=5
    local threshold=$(( STALE_KILL_SECS / interval ))
    while kill -0 "$ffpid" 2>/dev/null; do
        sleep "$interval"
        current_seg=$(ls -t "$HLS_DIR"/*.ts 2>/dev/null | head -1)
        current_size=0
        [ -n "$current_seg" ] && current_size=$(stat -c %s "$current_seg" 2>/dev/null || echo 0)
        if [ -n "$current_seg" ] && { [ "$current_seg" != "$last_seg" ] || [ "$current_size" -gt "$last_size" ]; }; then
            # New segment rolled over, or the in-progress one is still being
            # written — ffmpeg is alive and making progress.
            stale_count=0
            last_seg="$current_seg"
            last_size="$current_size"
        else
            stale_count=$((stale_count + 1))
        fi
        if [ $stale_count -ge $threshold ]; then
            log "  [WATCHDOG] No write progress for ${STALE_KILL_SECS}s – killing ffmpeg PID $ffpid"
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
# ALWAYS re-encode to H.264 540p (no stream-copy). Two source quirks force this:
#
# 1. Pacing: the IPTV source delivers a buffered backlog ~18x faster than
#    realtime. `-re` pins reading to the content's native rate so the HLS live
#    edge advances at 1x instead of racing ahead (which made players seize).
#
# 2. A/V timeline: the source's audio PTS wraps past the 33-bit MPEG-TS limit
#    on a different boundary than video, so a stream-COPY ends up with audio and
#    video tens-of-thousands of seconds apart. ffmpeg tolerates it, but browser
#    MSE/hls.js cannot align them and renders a black 0x0 frame (downloads
#    segments but never plays). Re-encoding lets us run setpts/asetpts to force
#    both streams onto a shared, zero-based timeline that every browser plays.
#
# NOTE: -re is incompatible with -use_wallclock_as_timestamps (together they
# emit ZERO segments); we rely on -fflags +genpts plus the setpts reset instead.
push_live() {
    ensure_hls_dir
    log "→ LIVE re-encoding to H.264 540p (A/V realigned, browser-safe)"
    ffmpeg -hide_banner -loglevel warning \
        -re \
        -fflags +igndts+discardcorrupt+genpts \
        -err_detect ignore_err \
        -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
        -headers "Referer: http://prosclan.fans/" \
        -reconnect 1 -reconnect_at_eof 1 \
        -reconnect_streamed 1 -reconnect_delay_max 5 \
        -timeout 10000000 \
        -i "$SOURCE_URL" \
        -vf "scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2,setpts=PTS-STARTPTS" \
        -c:v libx264 -preset ultrafast -crf 28 \
        -r "$FPS" -g "$GOP" -keyint_min "$GOP" \
        -force_key_frames "expr:gte(t,n_forced*4)" \
        -c:a aac -b:a "${AUDIO_BR}k" -ar 44100 \
        -af "aresample=async=1,asetpts=PTS-STARTPTS" \
        -avoid_negative_ts make_zero \
        -flush_packets 1 \
        -f hls -hls_time 4 -hls_list_size 40 \
        -hls_flags delete_segments+append_list+independent_segments \
        -hls_segment_type mpegts \
        -hls_segment_filename "$HLS_DIR/%d.ts" \
        "$HLS_DIR/index.m3u8" \
        2>&1 &

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
URL_IDX=0             # index into SOURCE_URLS of the currently-active source

while true; do
    SOURCE_URL="${SOURCE_URLS[$URL_IDX]:-}"
    if [[ -n "$SOURCE_URL" ]] && [[ $LIVE_FAIL -lt $MAX_FAILS ]]; then
        T_START=$(date +%s)
        push_live
        EXIT=$?
        T_RUN=$(( $(date +%s) - T_START ))
        if [[ $T_RUN -ge $HEALTHY_RUN_SECS ]]; then
            # Healthy stream hit a transient drop – reconnect to the SAME working
            # URL immediately, no penalty and no failover.
            LIVE_FAIL=0
            log "Live exited (code=$EXIT) ran=${T_RUN}s on source[$URL_IDX] – healthy, reconnecting"
        else
            # Fast failure (likely HTTP 509 / dead edge): rotate to the next
            # backup URL so the next attempt hits a different upstream node.
            LIVE_FAIL=$((LIVE_FAIL + 1))
            if [[ $NUM_URLS -gt 1 ]]; then
                URL_IDX=$(( (URL_IDX + 1) % NUM_URLS ))
                log "Live exited (code=$EXIT) ran=${T_RUN}s fail=$LIVE_FAIL/$MAX_FAILS – failover to source[$URL_IDX]"
            else
                log "Live exited (code=$EXIT) ran=${T_RUN}s fail=$LIVE_FAIL/$MAX_FAILS"
            fi
        fi
    else
        push_standby || true
        log "Standby ended – resetting to primary, retrying live"
        LIVE_FAIL=0
        URL_IDX=0   # after a standby cycle, start over from the primary URL
    fi
    sleep 1
done
