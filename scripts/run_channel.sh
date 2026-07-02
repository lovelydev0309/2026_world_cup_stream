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

# ── Reap orphaned ffmpeg from a prior instance ────────────────
# We hold the flock, so no other run_channel.sh for this channel is running.
# But a previous instance killed with SIGKILL (or a hard RPA restart) skips the
# trap above, leaving its ffmpeg child orphaned (re-parented to init) and still
# writing our HLS dir. Two+ ffmpeg rewriting one manifest corrupts it → the
# player loops on levelLoadError and shows a black frame. Kill any such strays
# now, before we start our own ffmpeg. Match the ffmpeg segment-filename arg
# (hls/<ch>/%d.ts or hls/<ch>/<num>.ts) — unique to this channel's ffmpeg and
# absent from any run_channel.sh command line, so nothing else is hit.
pkill -9 -f "hls/${CHANNEL}/[0-9%]" 2>/dev/null || true

# ── Read config ───────────────────────────────────────────────
# Scalar fields (standby/bitrate/fps/force_silent_audio) on one line …
read -r STANDBY_REL BITRATE AUDIO_BR FPS FORCE_SILENT_AUDIO AUDIO_SYNC VIDEO_MODE ENC_RES USE_STANDBY < <(python3 -c "
import json, sys
cfg = json.load(open('$CONFIG'))
chs = [c for c in cfg['channels'] if c['channel_name'] == '$CHANNEL']
if not chs: sys.exit('Channel not found')
c = chs[0]
print(c.get('standby_file','standby/standby.mp4'),
      c.get('bitrate',2500), c.get('audio_bitrate',128), c.get('fps',30),
      'true' if c.get('force_silent_audio') else 'false',
      c.get('audio_sync','regen'),
      c.get('video_mode','encode'),
      c.get('encode_resolution','960x540'),
      'true' if c.get('use_standby', True) else 'false')")
# Output resolution for re-encode mode (WxH). Default 960x540. Per-channel so the
# marquee feeds can run 720p while the heavy 60fps one stays 540p for CPU.
ENC_W=${ENC_RES%x*}; ENC_H=${ENC_RES#*x}
[ -z "$ENC_W" ] && ENC_W=960; [ -z "$ENC_H" ] && ENC_H=540

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
STALE_KILL_SECS=25   # kill ffmpeg after this many seconds of ZERO write progress.
# Was 45s, but the live3 player rides ~45s behind the edge (liveSyncDuration:45); a
# 45s stall-detect drains that whole cushion BEFORE we even kill+reconnect, so the
# viewer sees a freeze. 25s of zero byte-growth (≈6 missed 4s segments) is still a
# definitively-dead feed — ffmpeg's own -reconnect recovers real hiccups within
# ~5-8s and resumes byte-growth (resetting the counter), so this only ever fires on
# a genuinely frozen source. 25s detect + ~10s reconnect ≈ 35s gap < 45s cushion →
# failover completes while the player is still playing buffered content (no freeze).
# Periodic PTS reset: append_list carries the output PTS across failover restarts,
# so on a long-lived channel it climbs without bound. hls.js remuxes TS→MP4 with a
# 32-bit baseMediaDecodeTime; at 90kHz that overflows at 2^32/90000 ≈ 47,700s ≈
# 13.2h, after which audio/video land at wrapped, mismatched positions and the
# browser sees an empty buffer intersection → infinite "loading"/lag (observed on
# channel1 after long uptime). Every MAX_SESSION_SECS we wipe the manifest to
# zero-base the PTS again; well under the 13.2h ceiling, one brief reload apart.
MAX_SESSION_SECS=21600          # 6h — ~2.2x margin below the hls.js 13.2h ceiling
SESSION_FILE="$PROJECT_DIR/cache/session_${CHANNEL}"
mkdir -p "$PROJECT_DIR/cache" 2>/dev/null || true

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
    local last_num=-1 current_num race_count=0
    local interval=5
    local threshold=$(( STALE_KILL_SECS / interval ))
    # RACING guard: a runaway source clock (some IPTV feeds dump content many x
    # faster than realtime with bloated timestamps) makes ffmpeg emit segments in
    # a flood — the HLS live edge races away, the player can't keep up, and the
    # PTS sprints toward the 33-bit MPEG-TS wraparound. -re can't pace a source
    # whose own timestamps are wrong. So if >RACE_SEGS segments appear per
    # interval for RACE_HITS consecutive checks (sustained, not a brief post-
    # restart catch-up burst), kill ffmpeg — the main loop then falls back to the
    # paced standby clip instead of serving an unplayable flood.
    local RACE_SEGS=8     # >8 new segs in 5s ≈ >6x realtime
    local RACE_HITS=3     # ~15s sustained before acting
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
        # ── racing detection ──
        current_num=$(basename "${current_seg:-x}" .ts 2>/dev/null)
        case "$current_num" in ''|*[!0-9]*) current_num=-1 ;; esac
        if [ "$last_num" -ge 0 ] && [ "$current_num" -ge 0 ] && [ $(( current_num - last_num )) -gt $RACE_SEGS ]; then
            race_count=$((race_count + 1))
            if [ $race_count -ge $RACE_HITS ]; then
                log "  [WATCHDOG] racing $(( current_num - last_num )) segs/${interval}s (runaway source clock) – killing ffmpeg PID $ffpid"
                kill -9 "$ffpid" 2>/dev/null
                break
            fi
        else
            race_count=0
        fi
        last_num=$current_num
    done
}
WATCHDOG_PID=0

# ── Detect source codec (H.264 vs HEVC) ──────────────────────
# Caches result in /tmp/codec_${CHANNEL} so restarts don't re-probe.
# Falls back to cached value if probe times out (source temporarily down).
CODEC_CACHE="$PROJECT_DIR/cache/codec_${CHANNEL}"
AUDIO_CACHE="$PROJECT_DIR/cache/audio_${CHANNEL}"
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

# ── Detect source audio validity ─────────────────────────────
# Some IPTV feeds (notably channel2/313828) intermittently deliver a BROKEN
# audio stream: 0 channels and an invalid 1/0 time_base. That garbage stream
# poisons ffmpeg's pipeline timing — -re loses pacing so segments race out ~22x
# realtime (the live edge runs away and the player can't keep up) AND the output
# audio is undecodable by browsers. Returns success only if the source has at
# least one real audio channel; otherwise push_live swaps in clean silent stereo.
detect_audio_ok() {
    # CACHE the decision (cleared on a STANDBY cycle, where the source may have
    # changed — see the main loop). A source's audio config is stable, so probing
    # on every ~45s token-cadence reconnect is wasteful AND dangerous: when the
    # probe momentarily fails MID-RECONNECT (source between tokens) the old code
    # returned "no audio" and dropped to SILENT — the sound the client hears
    # cutting out. Probe once, then trust the cache; a transient probe failure
    # before any cache stays silent only that one time.
    if [ -f "$AUDIO_CACHE" ]; then
        [ "$(cat "$AUDIO_CACHE" 2>/dev/null)" = "ok" ]
        return
    fi
    local chans
    chans=$(timeout 5 ffprobe -v quiet -hide_banner \
        -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
        -analyzeduration 2000000 -probesize 1000000 \
        -select_streams a:0 -show_entries stream=channels \
        -of csv=p=0 "$SOURCE_URL" 2>/dev/null | head -1)
    if [ -n "$chans" ] && [ "$chans" -ge 1 ] 2>/dev/null; then
        echo ok > "$AUDIO_CACHE" 2>/dev/null; return 0     # confirmed real audio
    elif [ -n "$chans" ]; then
        echo no > "$AUDIO_CACHE" 2>/dev/null; return 1     # genuine 0-channel feed
    fi
    return 1                                               # transient fail, no cache yet
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

    # ── Periodic PTS reset (see MAX_SESSION_SECS note above) ─────────────
    # When the manifest "session" exceeds MAX_SESSION_SECS, wipe the HLS dir so
    # the next ffmpeg starts a fresh, zero-based timeline (keeps the PTS far below
    # hls.js's 13.2h 32-bit ceiling). -t SESSION_T below caps each ffmpeg so even
    # a perfectly stable feed gets reset on schedule, not just on a failover.
    local _now=$(date +%s)
    local _sess=$(cat "$SESSION_FILE" 2>/dev/null)
    [ -z "$_sess" ] && { _sess=$_now; echo "$_now" > "$SESSION_FILE"; }
    local _age=$(( _now - _sess ))
    if [ "$_age" -ge "$MAX_SESSION_SECS" ]; then
        log "  PTS-RESET: fresh manifest (session ${_age}s ≥ ${MAX_SESSION_SECS}s) – avoids hls.js 32-bit timestamp overflow"
        rm -f "$HLS_DIR"/*.ts "$HLS_DIR"/*.m3u8 2>/dev/null
        echo "$_now" > "$SESSION_FILE"; _age=0
    fi
    local SESSION_T=$(( MAX_SESSION_SECS - _age ))   # ffmpeg exits when the session hits MAX
    [ "$SESSION_T" -lt 60 ] && SESSION_T=60          # floor; never a 0/negative -t

    # Pick the audio path. Source audio is used when valid (channel1/3); when the
    # source delivers a broken 0-channel stream (channel2's feed does this and it
    # both breaks browser audio AND makes -re race at ~22x), discard it and feed
    # clean silent stereo from anullsrc instead so the channel still plays 1x.
    local aud_in=() aud_map=() aud_tail=()
    if [ "$FORCE_SILENT_AUDIO" != "true" ] && detect_audio_ok && [ "$AUDIO_SYNC" = "preserve" ]; then
        log "→ LIVE (source audio, timestamps PRESERVED)"
        # PRESERVE mode — for sources whose A/V is ALIGNED at the source but which
        # OVER-DELIVER audio (send a backlog faster than realtime, e.g. VIX Canal
        # 5). The default regen path (asetpts=N/SR/TB) rebuilds the audio PTS from
        # the decoded SAMPLE COUNT; the surplus samples inflate that count so the
        # audio races ahead and eventually drifts hours past the video (channel2
        # drifted ~26h → browser could not align → would not start). -re already
        # paces the INPUT to realtime by the source PTS, so we KEEP the source's
        # aligned timestamps and just zero-base them (parallel to the video's
        # setpts=PTS-STARTPTS) instead of regenerating from sample count.
        aud_tail=(-af "asetpts=PTS-STARTPTS")
    elif [ "$FORCE_SILENT_AUDIO" != "true" ] && detect_audio_ok; then
        log "→ LIVE (source audio, A/V realigned)"
        # Regenerate the audio PTS from the REAL decoded SAMPLE COUNT
        # (asetpts=N/SR/TB) so it's pinned to realtime and locked to the video,
        # immune to a runaway source audio clock (which otherwise raced the audio
        # tens-of-thousands of seconds ahead of video and wrapped the 33-bit
        # MPEG-TS limit → browser stuck on "Loading…"). Used when the SOURCE
        # timestamps themselves are unreliable. For aligned-but-over-delivering
        # feeds use audio_sync="preserve" instead (above).
        aud_tail=(-af "asetpts=N/SR/TB")
    else
        log "→ LIVE (source audio broken – silent stereo)"
        aud_in=(-f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=44100")
        aud_map=(-map 0:v:0 -map 1:a:0)
        aud_tail=(-shortest)
    fi

    # ── Video path: COPY (native HD, ~0 CPU) vs re-encode 540p ──────────
    # video_mode=copy stream-COPIES the source's H.264 video at native resolution
    # (1080p / 720p / 720p60) for near-zero CPU — the big quality win on this
    # 2-vCPU, no-HW-encoder box. The 540p libx264 path is the DEFAULT and the
    # instant per-channel rollback (video_mode=encode). Audio is re-encoded to
    # AAC in BOTH modes (never -c:a copy — copying both raw streams re-exposes
    # the 33-bit-PTS-wrap 0x0 black-frame bug this pipeline was written to avoid).
    local vid_args=()
    if [ "$VIDEO_MODE" = "copy" ]; then
        log "  video_mode=COPY (native-resolution H.264 passthrough, ~0 CPU)"
        vid_args=(-c:v copy)
        # Copy keeps the source video timeline, so re-encoded audio aligns BEST
        # with NO PTS filter (measured A/V 0.13s, vs 0.33s with asetpts which
        # zeros the audio independently of the copied video). Drop the asetpts
        # tail for the source-audio case (aud_in empty == not the silent branch).
        [ ${#aud_in[@]} -eq 0 ] && aud_tail=()
    else
        vid_args=(-vf "scale=${ENC_W}:${ENC_H}:force_original_aspect_ratio=decrease,pad=${ENC_W}:${ENC_H}:(ow-iw)/2:(oh-ih)/2,setpts=PTS-STARTPTS" \
                  -c:v libx264 -preset ultrafast -crf 24 -threads 2 \
                  -r "$FPS" -g "$GOP" -keyint_min "$GOP" \
                  -force_key_frames "expr:gte(t,n_forced*4)")
    fi

    ffmpeg -hide_banner -loglevel warning \
        -re \
        -fflags +igndts+discardcorrupt+genpts \
        -err_detect ignore_err \
        -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
        -reconnect 1 -reconnect_at_eof 1 \
        -reconnect_streamed 1 -reconnect_delay_max 5 \
        -timeout 10000000 \
        -i "$SOURCE_URL" \
        "${aud_in[@]}" \
        "${aud_map[@]}" \
        "${vid_args[@]}" \
        -c:a aac -b:a "${AUDIO_BR}k" -ar 44100 \
        "${aud_tail[@]}" \
        -avoid_negative_ts make_zero -muxpreload 0 -muxdelay 0 \
        -t "$SESSION_T" \
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
        if [ "$USE_STANDBY" = "false" ]; then
            # Copy-mode channels skip the re-encoded standby clip: its SPS/PPS differ
            # from the copied native stream, so a live↔standby swap triggers hls.js
            # bufferAppendError. Instead just hold briefly and retry live (the source
            # is a stable mainstream feed; drops are rare and self-heal on reconnect).
            log "Live unavailable – no-standby (copy) mode; brief hold, retrying live"
            sleep 5
        else
            push_standby || true
            log "Standby ended – resetting to primary, retrying live"
        fi
        LIVE_FAIL=0
        URL_IDX=0   # after a standby/hold cycle, start over from the primary URL
        rm -f "$AUDIO_CACHE" 2>/dev/null   # source may have changed → re-probe audio
    fi
    sleep 1
done
