#!/usr/bin/env bash
# run_channel.sh – Pull IPTV source → HLS with stale-segment watchdog.
set -uo pipefail

# RPA used to leave its flock fd inherited; close it so we never pin that lock.
exec 8>&- 2>/dev/null || true

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
# CRITICAL: every child spawned below (ffmpeg, the watchdog subshell) MUST close fd 9
# (they run with `9>&-`). A child otherwise inherits this locked fd, and if the script is
# SIGKILL'd (RPA hard-restart / crash — the EXIT trap can't run), the orphaned child KEEPS
# the flock forever. Every new instance then fails the flock below, logs "Already running",
# and exits WITHOUT reaching the orphan-reap → the channel is stuck dead and never
# auto-recovers (root cause of "channel X won't come back"). Closing fd 9 in the children
# lets a fresh instance take the lock and reap the orphan.
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
read -r STANDBY_REL BITRATE AUDIO_BR FPS FORCE_SILENT_AUDIO AUDIO_SYNC VIDEO_MODE ENC_RES USE_STANDBY SEGMENT_TYPE TOKEN_REFRESH_SECS HLS_TIME < <(python3 -c "
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
      'true' if c.get('use_standby', True) else 'false',
      c.get('segment_type','mpegts'),
      c.get('token_refresh_secs', 0),
      c.get('hls_time', 4))")
[ -z "$HLS_TIME" ] && HLS_TIME=4
case "$HLS_TIME" in ''|*[!0-9]*) HLS_TIME=4 ;; esac
[ "$HLS_TIME" -lt 2 ] && HLS_TIME=2
[ "$HLS_TIME" -gt 6 ] && HLS_TIME=6
# Proactive token refresh: tvon247's 302 token silently expires (socket goes quiet, no EOF),
# and ffmpeg's -reconnect can't recover it — only a process RESTART re-resolves the 302 for a
# FRESH token. Reactively that costs a ~15s watchdog gap. A planned -t cap every 180s was
# worse: each restart zeroed PTS, wrote #EXT-X-DISCONTINUITY, and hls.js looped 0×0 /
# fragLoadTimeOut. Default 0 = run until the watchdog sees a true stall. Azteca Uno still
# overrides token_refresh_secs=35 in channels.json because its token dies in ~15-45s.
[ -z "$TOKEN_REFRESH_SECS" ] && TOKEN_REFRESH_SECS=0
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

# ── Resolve provider-account placeholders (@@ALIAS@@ → host/user/pass) ─────────
# Credentials are NOT stored in channels.json (that file is committed to a public
# repo). Each source URL carries an @@ALIAS@@ token; the real host/user/pass lives
# ONLY in the untracked config/accounts.env (gitignored, deployed out-of-band).
# We substitute here so ffmpeg receives a real URL while git only ever sees the alias.
ACCOUNTS_ENV="$PROJECT_DIR/config/accounts.env"
if [ -f "$ACCOUNTS_ENV" ]; then
    declare -A ACCT
    while IFS='=' read -r _k _v; do
        [[ "$_k" =~ ^[A-Za-z0-9_]+$ ]] || continue   # skip comments / blank lines
        ACCT["$_k"]="$_v"
    done < "$ACCOUNTS_ENV"
    for _i in "${!SOURCE_URLS[@]}"; do
        _u="${SOURCE_URLS[$_i]}"
        for _k in "${!ACCT[@]}"; do
            _u="${_u//@@${_k}@@/${ACCT[$_k]}}"
        done
        SOURCE_URLS[$_i]="$_u"
    done
    unset ACCT
else
    log "  [FATAL] $ACCOUNTS_ENV missing — cannot resolve provider credentials for $CHANNEL"
fi

SOURCE_URL="${SOURCE_URLS[0]:-}"   # active URL; rotated by the main loop on failover

STANDBY="$PROJECT_DIR/$STANDBY_REL"
HLS_DIR="$PROJECT_DIR/hls/$CHANNEL"

# HLS segment packaging (used by push_live AND push_standby): mpegts (.ts, default)
# or fmp4/CMAF (.m4s + init.mp4). fmp4 plays natively in hls.js (no 32-bit TS→MP4
# remux) → no long-uptime timestamp overflow → no periodic PTS-reset wipe, so the
# viewer never hits the "buffer→0, video stops after hours" freeze.
# omit_endlist (BOTH segment types): these tokenized sources restart every ~90-285s
# (and drop to standby when a feed degrades). Without omit_endlist, ffmpeg writes
# #EXT-X-ENDLIST every time it EXITS — during any restart gap or live→standby gap the
# live manifest briefly reads "stream ended", and a player that polls in that window
# STOPS and will not resume without a manual reload (this is the client-reported "It
# has stopped" on a flaky channel). With omit_endlist the manifest stays open, so the
# player keeps polling and AUTO-RESUMES the instant fresh segments reappear — a source
# outage becomes a recoverable "buffering" instead of a dead "stopped". append_list
# re-opens the manifest on the next start regardless, so a 24/7 channel never needs an
# ENDLIST. (Was only on the fmp4 branch; the production mpegts path was missing it.)
if [ "$SEGMENT_TYPE" = "fmp4" ]; then
    HLS_SEG=(-hls_segment_type fmp4 -hls_fmp4_init_filename init.mp4 -hls_segment_filename "$HLS_DIR/%d.m4s")
    # Re-encoding with fixed libx264 params makes init.mp4 byte-identical across restarts
    # (verified), so overwriting it each restart is harmless and old segments stay decodable.
    # temp_file: write each segment + the playlist to a .tmp file and atomically rename it
    # into place only once fully written. Without it, ffmpeg rewrites index.m3u8 IN PLACE, so
    # the no-cache CDN can pull it mid-rewrite and serve an EMPTY/partial manifest (observed on
    # channel2) → the player briefly has no segments → "plays without video" / stalls. Also
    # stops the CDN ever serving a half-written .ts (decode errors).
    HLS_FLAGS="delete_segments+append_list+independent_segments+omit_endlist+temp_file"
else
    HLS_SEG=(-hls_segment_type mpegts -hls_segment_filename "$HLS_DIR/%d.ts")
    # temp_file: write each segment + the playlist to a .tmp file and atomically rename it
    # into place only once fully written. Without it, ffmpeg rewrites index.m3u8 IN PLACE, so
    # the no-cache CDN can pull it mid-rewrite and serve an EMPTY/partial manifest (observed on
    # channel2) → the player briefly has no segments → "plays without video" / stalls. Also
    # stops the CDN ever serving a half-written .ts (decode errors).
    HLS_FLAGS="delete_segments+append_list+independent_segments+omit_endlist+temp_file"
fi
GOP=$((FPS * 2))
# ── Match output fps to the SOURCE cadence ───────────────────────────────────
# Hard-forcing 30.000 on a 59.94fps or 25fps feed drops/duplicates frames UNEVENLY →
# visible video judder (audio unaffected — the "video stutters, not buffering" report).
# Probe the source rate ONCE and output a clean match: the source fps halved while >32
# (so we stay ~24-30fps, CPU-neutral) with an EXACT integer ratio (59.94→29.97, 50→25,
# 25→25) instead of a lossy 30.000 resample. Falls back to the config fps if probe fails.
OUT_FPS="$FPS"; OUT_GOP="$GOP"
_srcfps=$(timeout 12 ffprobe -v error -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
    -analyzeduration 4M -probesize 4M -select_streams v:0 -show_entries stream=r_frame_rate \
    -of csv=p=0 "${SOURCE_URLS[0]}" 2>/dev/null | head -1)
if [ -n "$_srcfps" ]; then
    _out=$(python3 - "$_srcfps" <<'PY'
import sys
try:
    n, d = (sys.argv[1].split('/') + ['1'])[:2]; n, d = int(n), int(d)
    assert n > 0 and d > 0
    while n / d > 32: n //= 2       # exact-ratio decimation down to ~24-30fps
    print("%d/%d %d" % (n, d, max(1, round(n / d)) * 2))
except Exception:
    pass
PY
)
    [ -n "$_out" ] && { OUT_FPS="${_out% *}"; OUT_GOP="${_out#* }"; }
fi
log "  output fps=$OUT_FPS gop=$OUT_GOP (source cadence ${_srcfps:-unknown})"
STALE_KILL_SECS=8   # kill ffmpeg after this many seconds of ZERO write progress.
# Root cause of the "buffer→0 on some channels" reports: the tvon247 sources are
# 302-redirect tokenized feeds whose token expires every ~90-285s. On expiry the
# upstream keeps the TCP socket OPEN but stops sending data (no EOF, no error), so
# ffmpeg's -reconnect/-reconnect_at_eof never fire — the ONLY recovery is this
# watchdog killing ffmpeg, which forces a process restart that re-resolves the 302
# and gets a FRESH token. So the watchdog gap IS the viewer-visible gap. Was 25s
# (→ ~30s gap) then 15s. Lowered to 10s (2026-07-09): some feeds (notably Azteca
# Uno / ch5, stream 1028329) have an unusually SHORT token TTL (~50-110s on EVERY
# account, not fixable by failover), so they token-stall every ~1-2 min; each 15s
# recovery drained the cushion faster than the player's 0.94x maintainCushion could
# rebuild it → the classic Azteca Uno buffer→0. Every channel is encode-mode with
# 4s segments and ffmpeg's -reconnect recovers REAL network hiccups within 5-8s
# (resuming byte-growth, which resets this counter), so 10s keeps a 2s margin above
# that and still never false-kills a live feed — it only shortens the unrecoverable
# token-stall gap: 10s detect + ~5s restart ≈ 15s (was ~20s), which meaningfully
# slows the cushion drain on short-token channels. Absorbed by a player buffered
# ≥30s behind the edge (see client player-config note).
# Periodic PTS reset: append_list carries the output PTS across failover restarts,
# so on a long-lived channel it climbs without bound. hls.js remuxes TS→MP4 with a
# 32-bit baseMediaDecodeTime; at 90kHz that overflows at 2^32/90000 ≈ 47,700s ≈
# 13.2h, after which audio/video land at wrapped, mismatched positions and the
# browser sees an empty buffer intersection → infinite "loading"/lag (observed on
# channel1 after long uptime). Every MAX_SESSION_SECS we wipe the manifest to
# zero-base the PTS again; well under the 13.2h ceiling, one brief reload apart.
MAX_SESSION_SECS=39600          # 11h — under the hls.js 13.2h TS remux ceiling; 6h resets were QA “Other”
SESSION_FILE="$PROJECT_DIR/cache/session_${CHANNEL}"
# Carry the output timeline across token-refresh restarts. Each ffmpeg run used
# to zero PTS (setpts=PTS-STARTPTS), so the HLS muxer wrote #EXT-X-DISCONTINUITY
# every ~3 min. hls.js then jumps to the new period at the live edge, the next
# .ts is not there yet, and the player loops fragLoadTimeOut / aborted.
PTS_OFFSET_FILE="$PROJECT_DIR/cache/pts_offset_${CHANNEL}"
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

# Invariant: every published segment MUST contain a video stream.
# Audio-only .ts appended onto a video playlist is what produced Imagen's
# mediaError/bufferAppendError + 0×0 loop. Size is a fast hint; ffprobe is truth.
MIN_VIDEO_SEG=200000

wipe_hls() {
    rm -f "$HLS_DIR"/*.ts "$HLS_DIR"/*.m4s "$HLS_DIR"/*.m3u8 "$HLS_DIR"/init.mp4 2>/dev/null
    rm -f "$PTS_OFFSET_FILE" 2>/dev/null
}

completed_seg() {
    ls -t "$HLS_DIR"/*.ts "$HLS_DIR"/*.m4s 2>/dev/null | sed -n 2p
}

segment_has_video() {
    local f="$1" v
    [ -n "$f" ] && [ -f "$f" ] || return 1
    v=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name -of csv=p=0 "$f" 2>/dev/null | head -1)
    [ -n "$v" ]
}

# Kill any OTHER ffmpeg still writing this channel's HLS dir (orphaned standby
# after SIGKILL, or a second run_channel). Never use pkill -f "channel1" — it
# matches channel10-19.
reap_stray_ffmpeg() {
    local keep="${1:-0}" pid cmd
    for pid in $(ls -d /proc/[0-9]* 2>/dev/null | sed 's|.*/||'); do
        [ "$pid" = "$keep" ] && continue
        [ -r "/proc/$pid/cmdline" ] || continue
        cmd=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)
        case "$cmd" in
            *ffmpeg*"hls/${CHANNEL}/"*) kill -9 "$pid" 2>/dev/null || true ;;
        esac
    done
}

playlist_is_novideo() {
    local f
    f=$(completed_seg)
    [ -n "$f" ] || return 1
    # Size is not truth: Telesur/news and 2s segs are often 200–600KB WITH video.
    # Wiping those was a false novideo → playlist hole → QA “Other”.
    if ! segment_has_video "$f"; then
        return 0
    fi
    return 1
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
    # First 4s encode segment often takes >10s wall-clock (1080p→540p, 70+ encoders).
    # Leftover .ts from the previous ffmpeg also sits at a fixed size, so the old
    # "newest file not growing" check false-kills a healthy new encode at ~15s and
    # the channel looks like it "went down" even when the provider is fine.
    local elapsed=0 seen_progress=0 tiny_count=0
    local STARTUP_GRACE_SECS=25
    local MIN_VIDEO_SEG=200000   # 540p 4s is ~1MB; AAC-only is ~90KB → player 0×0 / bufferAppendError
    # Seed from leftover HLS so the first check does not treat an old .ts as "progress".
    last_seg=$(ls -t "$HLS_DIR"/*.ts "$HLS_DIR"/*.m4s 2>/dev/null | head -1)
    [ -n "$last_seg" ] && last_size=$(stat -c %s "$last_seg" 2>/dev/null || echo 0)
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
        elapsed=$((elapsed + interval))
        # Match BOTH mpegts (.ts) and fmp4/CMAF (.m4s) so this watchdog tracks write
        # progress regardless of segment_type (a .ts-only check would see no growth on
        # an fmp4 channel and kill ffmpeg every cycle).
        current_seg=$(ls -t "$HLS_DIR"/*.ts "$HLS_DIR"/*.m4s 2>/dev/null | head -1)
        current_size=0
        [ -n "$current_seg" ] && current_size=$(stat -c %s "$current_seg" 2>/dev/null || echo 0)
        if [ -n "$current_seg" ] && { [ "$current_seg" != "$last_seg" ] || [ "$current_size" -gt "$last_size" ]; }; then
            # New segment rolled over, or the in-progress one is still being
            # written — ffmpeg is alive and making progress.
            seen_progress=1
            stale_count=0
            last_seg="$current_seg"
            last_size="$current_size"
        else
            stale_count=$((stale_count + 1))
        fi
        # Do not kill until this ffmpeg has written something, or startup grace expired.
        if [ "$seen_progress" -eq 0 ] && [ "$elapsed" -lt "$STARTUP_GRACE_SECS" ]; then
            stale_count=0
            continue
        fi
        if [ $stale_count -ge $threshold ]; then
            log "  [WATCHDOG] No write progress for ${STALE_KILL_SECS}s – killing ffmpeg PID $ffpid"
            kill -9 "$ffpid" 2>/dev/null
            break
        fi
        # Audio-only after warmup: source dropped video but audio still grows, so
        # the stale timer never fires and the player loops 0×0 / bufferAppendError.
        if [ "$seen_progress" -eq 1 ] && [ "$elapsed" -ge "$STARTUP_GRACE_SECS" ]; then
            local done_seg
            done_seg=$(completed_seg)
            if [ -n "$done_seg" ] && ! segment_has_video "$done_seg"; then
                tiny_count=$((tiny_count + 1))
                if [ "$tiny_count" -ge 2 ]; then
                    log "  [WATCHDOG] no video in $(basename "$done_seg") – wiping playlist and killing ffmpeg PID $ffpid"
                    wipe_hls
                    kill -9 "$ffpid" 2>/dev/null
                    break
                fi
            else
                tiny_count=0
            fi
        fi
        # ── racing detection ──
        current_num=$(basename "${current_seg:-x}" 2>/dev/null); current_num=${current_num%.*}
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
    # Only a confirmed-OK result is cached AND trusted. A source's audio config is stable, so
    # once we have SEEN real audio we never re-probe — that avoids the mid-reconnect probe miss
    # (source between tokens) that used to cut good sound out.
    #
    # A "no"/silent verdict is DELIBERATELY NOT trusted or latched: we re-probe it on every
    # reconnect so a feed that RECOVERS its audio track regains sound within one ~45-285s token
    # cycle. The OLD code cached "no" and only re-probed after a STANDBY cycle, so an
    # audio-broken feed that healed while staying LIVE (no standby) stayed muted for HOURS —
    # the "Imagen: video ok, audio silent for a long time" bug. Now silence self-heals.
    if [ -f "$AUDIO_CACHE" ] && [ "$(cat "$AUDIO_CACHE" 2>/dev/null)" = "ok" ]; then
        return 0
    fi
    # Not yet confirmed (no cache, or a prior silent verdict): probe, retrying a few times so a
    # single transient miss doesn't needlessly mute a feed that does have audio. A DEFINITIVE
    # 0-channel answer breaks immediately (no wasted retries / extra connections); only an
    # EMPTY probe result (the truly transient case) is retried.
    local chans i
    for i in 1 2 3; do
        chans=$(timeout 8 ffprobe -v quiet -hide_banner \
            -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
            -analyzeduration 3000000 -probesize 3000000 \
            -select_streams a:0 -show_entries stream=channels \
            -of csv=p=0 "$SOURCE_URL" 2>/dev/null | head -1)
        if [ -n "$chans" ] && [ "$chans" -ge 1 ] 2>/dev/null; then
            echo ok > "$AUDIO_CACHE" 2>/dev/null; return 0   # real audio → cached + trusted
        fi
        [ -n "$chans" ] && break     # probe returned "0" → genuine no-audio, don't retry
        sleep 1                       # probe returned nothing (transient) → retry
    done
    # Genuine 0-channel audio, or the probe failed every retry: serve silent THIS attempt only.
    # Never leave a trusted "no" behind — the fast-path trusts only "ok", so the next reconnect
    # re-probes and recovery is automatic, no standby required.
    rm -f "$AUDIO_CACHE" 2>/dev/null
    return 1
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
    reap_stray_ffmpeg

    # ── Periodic PTS reset (SEAMLESS – see MAX_SESSION_SECS note above) ──
    # hls.js remuxes TS→fMP4 and accumulates a 32-bit baseMediaDecodeTime that
    # overflows near 13.2h of uptime. We bound it every MAX_SESSION_SECS, but the OLD
    # way (rm the whole HLS dir) deleted the segments a live player had ALREADY buffered
    # → an instant buffer→0 / rebuffer on EVERY channel every 6h, which no player buffer
    # depth can absorb (the bytes are gone). The new way KEEPS the segments and emits a
    # single #EXT-X-DISCONTINUITY at the boundary (discont_start): hls.js re-anchors its
    # remux timeline to ~0 (same overflow protection) while the player plays STRAIGHT
    # THROUGH the tag with no rebuffer. -t SESSION_T caps each ffmpeg so even a perfectly
    # stable feed hits the boundary on schedule.
    local _now=$(date +%s)
    local _sess=$(cat "$SESSION_FILE" 2>/dev/null)
    [ -z "$_sess" ] && { _sess=$_now; echo "$_now" > "$SESSION_FILE"; }
    local _age=$(( _now - _sess ))
    local SESSION_T
    local PTS_RESET_FLAGS=""    # gets "+discont_start" for the one boundary-crossing run
    if [ "$SEGMENT_TYPE" = "fmp4" ]; then
        # fMP4/CMAF uses 64-bit timestamps and hls.js plays it natively (no 32-bit
        # TS→MP4 remux), so there is NO overflow — the PTS-reset wipe (and its
        # viewer-visible stop after long uptime) is ELIMINATED. Just refresh ffmpeg
        # every 12h via a seamless append_list restart (manifest continues, no wipe).
        # Switch guard: if stale mpegts .ts + an mpegts manifest are present (channel
        # was just flipped mpegts→fmp4), append_list would try to mix .ts and .m4s in
        # ONE playlist → ffmpeg exits immediately in a restart loop (this is what bit
        # the first channel3 attempt). Wipe ONCE for a clean CMAF start; steady-state
        # fmp4 restarts (only .m4s present) skip this and append_list continues.
        if ls "$HLS_DIR"/*.ts >/dev/null 2>&1; then
            log "  fmp4: clearing stale mpegts segments for a clean CMAF manifest"
            rm -f "$HLS_DIR"/*.ts "$HLS_DIR"/*.m3u8 "$HLS_DIR"/init.mp4 2>/dev/null
        fi
        SESSION_T=43200
    else
        # Symmetric switch guard: if stale fmp4 .m4s/init are present (channel was
        # just flipped fmp4→mpegts), wipe so append_list doesn't mix .m4s + .ts in
        # one playlist (→ immediate-exit loop, the reverse of the fmp4 switch case).
        if ls "$HLS_DIR"/*.m4s >/dev/null 2>&1; then
            log "  mpegts: clearing stale fmp4 segments for a clean mpegts manifest"
            rm -f "$HLS_DIR"/*.m4s "$HLS_DIR"/init.mp4 "$HLS_DIR"/*.m3u8 2>/dev/null
        fi
        if [ "$_age" -ge "$MAX_SESSION_SECS" ]; then
            # SEAMLESS: keep the segments, just flag an #EXT-X-DISCONTINUITY on this run
            # (append_list preserves the buffered .ts; delete_segments + hls_list_size
            # roll the old ones off within ~160s, so no disk bloat and no 404).
            log "  PTS-RESET (seamless): #EXT-X-DISCONTINUITY at session ${_age}s ≥ ${MAX_SESSION_SECS}s – re-anchors hls.js, no viewer rebuffer"
            PTS_RESET_FLAGS="+discont_start"
            echo "$_now" > "$SESSION_FILE"; _age=0
            echo 0 > "$PTS_OFFSET_FILE"
        fi
        SESSION_T=$(( MAX_SESSION_SECS - _age ))   # ffmpeg exits when the session hits MAX
        [ "$SESSION_T" -lt 60 ] && SESSION_T=60          # floor; never a 0/negative -t
    fi

    # Cap this ffmpeg run at the SOONER of the session boundary and the proactive token-refresh
    # interval, so a fresh 302 token is pulled BEFORE the current one silently dies. FFMPEG_CAP
    # is a GLOBAL (no `local`) read by the main loop to tell a PLANNED exit (token refresh or 6h
    # reset — reconnect on the SAME url) apart from a real fast-failure (which fails over).
    FFMPEG_CAP=$SESSION_T
    if [ "${TOKEN_REFRESH_SECS:-0}" -gt 0 ] && [ "$TOKEN_REFRESH_SECS" -lt "$FFMPEG_CAP" ]; then
        FFMPEG_CAP=$TOKEN_REFRESH_SECS
    fi

    # Seconds to add to this run's zero-based setpts so MPEG-TS time continues
    # from the previous ffmpeg instead of jumping to 0 (which forces DISCONTINUITY).
    local PTS_OFF=0
    if [ -z "$PTS_RESET_FLAGS" ] && [ -f "$PTS_OFFSET_FILE" ]; then
        PTS_OFF=$(awk '{v=$1+0; if (v<0 || v>39600) v=0; printf "%.6f", v}' "$PTS_OFFSET_FILE" 2>/dev/null || echo 0)
    fi
    [ -z "$PTS_OFF" ] && PTS_OFF=0
    local pts_bsf=()
    if awk -v o="$PTS_OFF" 'BEGIN{exit !(o>0.05)}'; then
        log "  pts-continue +${PTS_OFF}s (no discontinuity on token refresh)"
        # Shift encoded packet timestamps at the muxer. Putting the offset in
        # setpts made -r fill the 0→offset gap with duplicate frames and ffmpeg
        # exited in ~9s. -output_ts_offset is ignored by the HLS mpegts muxer.
        pts_bsf=(-bsf:v "setts=pts=PTS+${PTS_OFF}/TB:dts=DTS+${PTS_OFF}/TB"
                 -bsf:a "setts=pts=PTS+${PTS_OFF}/TB:dts=DTS+${PTS_OFF}/TB")
    fi

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
        # Require a video stream. Without -map, a source that drops video keeps
        # publishing AAC-only segments; hls.js then hits bufferAppendError / 0×0
        # because the playlist still claims a video+audio rendition.
        aud_map=(-map 0:v:0 -map 0:a:0)
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
        aud_map=(-map 0:v:0 -map 0:a:0)
    else
        # Mute-on-air is a QA “Other”. If another account/feed exists, fail over
        # instead of publishing silent stereo as if it were the live channel.
        if [ "$FORCE_SILENT_AUDIO" != "true" ] && [ "${NUM_URLS:-0}" -gt 1 ] && [ "${LIVE_FAIL:-0}" -lt "$NUM_URLS" ]; then
            log "→ LIVE skipped (no usable audio) – failover without mute slate"
            return 75
        fi
        log "→ LIVE (source audio broken – silent stereo)"
        aud_in=(-f lavfi -i "anullsrc=channel_layout=stereo:sample_rate=48000")
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
                  -c:v libx264 -preset ultrafast -crf 24 -maxrate 2000k -bufsize 4000k -threads 1 \
                  -r "$OUT_FPS" -g "$OUT_GOP" -keyint_min "$OUT_GOP" \
                  -force_key_frames "expr:gte(t,n_forced*${HLS_TIME})")
    fi

    # Audio is encoded at 48kHz (-ar 48000, below) to MATCH the source: tvon247
    # feeds are AAC 48000. Was 44100 → forced a double resample (source 48k→our
    # 44.1k, then the browser's 48kHz pipeline resamples 44.1k→48k on playback);
    # two independent-clock resamples accumulate artifacts → audible stutter after
    # a few minutes on EVERY channel. 48k in = 48k out = no resample anywhere.
    #
    # -ac 2 pins the output to CONSTANT stereo. Without it the encoded channel
    # count just follows the source, so a failover from a stereo primary to a mono
    # or 5.1 backup feed flips the output audio config (channels) MID-PLAYLIST.
    # hls.js initialises its MSE audio SourceBuffer once from the first segment and
    # cannot re-negotiate channel count → audio silently dies while video keeps
    # playing, and only switching channels (fresh MSE) brings it back — the "many
    # channels lose audio" report. Forcing stereo (48k AAC-LC) makes the audio
    # config identical across every source, failover, and the live↔standby swap.
    ffmpeg -hide_banner -loglevel warning \
        -re \
        -fflags +igndts+discardcorrupt+genpts \
        -err_detect ignore_err \
        -analyzeduration 1000000 -probesize 1000000 \
        -user_agent "IPTV Smarters/1.0 Dalvik/2.1.0" \
        -reconnect 1 -reconnect_at_eof 1 \
        -reconnect_streamed 1 -reconnect_on_network_error 1 \
        -reconnect_delay_max 2 \
        -rw_timeout 8000000 \
        -i "$SOURCE_URL" \
        "${aud_in[@]}" \
        "${aud_map[@]}" \
        "${vid_args[@]}" \
        -c:a aac -b:a "${AUDIO_BR}k" -ar 48000 -ac 2 \
        "${aud_tail[@]}" \
        -avoid_negative_ts make_zero -muxpreload 0 -muxdelay 0 \
        "${pts_bsf[@]}" \
        -t "$FFMPEG_CAP" \
        -flush_packets 1 \
        -f hls -hls_time "$HLS_TIME" -hls_list_size 40 \
        -hls_flags "${HLS_FLAGS}${PTS_RESET_FLAGS}" \
        "${HLS_SEG[@]}" \
        "$HLS_DIR/index.m3u8" \
        2>&1 9>&- &

    local FPID=$!
    echo $FPID > "$FFMPEG_PID_FILE"
    # Fresh watchdog per FFmpeg run — no stale state from old segments
    stale_watchdog "$FPID" 9>&- &
    WATCHDOG_PID=$!
    wait $FPID
    local _frc=$?
    kill $WATCHDOG_PID 2>/dev/null; wait $WATCHDOG_PID 2>/dev/null
    rm -f "$FFMPEG_PID_FILE"
    # Last packet PTS already includes this run's offset; next run continues from here.
    local _last _pts
    _last=$(ls -t "$HLS_DIR"/*.ts "$HLS_DIR"/*.m4s 2>/dev/null | head -1)
    if [ -n "$_last" ] && [ -f "$_last" ]; then
        _pts=$(ffprobe -v error -select_streams v:0 -show_entries packet=pts_time -of csv=p=0 "$_last" 2>/dev/null | awk 'NF{p=$1} END{print p}')
        if [ -n "$_pts" ]; then
            echo "$_pts" > "$PTS_OFFSET_FILE"
        fi
    fi
    return $_frc
}

# ── Standby: 30s cycle of standby.mp4 ─────────────────────────
push_standby() {
    ensure_hls_dir
    reap_stray_ffmpeg
    log "→ STANDBY"
    # Scale standby to the channel's live output resolution (${ENC_W}x${ENC_H}).
    # standby.mp4 is 1080p; without this the standby segments differ in resolution
    # from the live re-encode. For mpegts that's just a cosmetic resolution change,
    # but for fmp4 it would emit a MISMATCHED init.mp4 (SPS/PPS) and break playback
    # across a live↔standby transition — so match the live encode here.
    ffmpeg -hide_banner -loglevel warning \
        -re -stream_loop -1 -i "$STANDBY" \
        -vf "scale=${ENC_W}:${ENC_H}:force_original_aspect_ratio=decrease,pad=${ENC_W}:${ENC_H}:(ow-iw)/2:(oh-ih)/2,setpts=PTS-STARTPTS" \
        -c:v libx264 -preset ultrafast -crf 26 \
        -r "$OUT_FPS" -g "$OUT_GOP" \
        -force_key_frames "expr:gte(t,n_forced*2)" \
        -c:a aac -b:a "${AUDIO_BR}k" -ar 48000 -ac 2 \
        -t 30 \
        -f hls -hls_time 2 -hls_list_size 20 \
        -hls_flags "$HLS_FLAGS" \
        "${HLS_SEG[@]}" \
        "$HLS_DIR/index.m3u8" \
        2>&1 9>&- &
    local FPID=$!
    echo $FPID > "$FFMPEG_PID_FILE"
    stale_watchdog "$FPID" 9>&- &
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
# Try EVERY source once before dropping to standby. Was a fixed 3 (set when channels had
# ~3 sources); with the 6-account expansion a channel now has 6+ sources, and a hard-coded 3
# meant the failover gave up after only the first 3 accounts — never trying the other 3
# (incl. the 2 new accounts), even if one of them had a working feed. Scaling to NUM_URLS
# means a degraded feed on some accounts fails over across ALL of them before showing slate.
MAX_FAILS=$((NUM_URLS * 2))
HEALTHY_RUN_SECS=45   # a run at least this long = healthy, resets the fail counter
URL_IDX=0             # index into SOURCE_URLS of the currently-active source
LAST_MODE=""          # "live"|"standby" — for fmp4, wipe on live↔standby transitions
                      # (their init.mp4 differ; a fixed-name init would mismatch)

while true; do
    # ── Drift self-correction at the 6h PTS-reset boundary ──────────────────
    # The "healthy, reconnecting on SAME URL" path below means a channel that
    # failed over to a backup account and then ran fine STAYS on that backup
    # indefinitely — it never returns to its primary. Over long uptimes the live
    # account distribution therefore DRIFTS off the balanced primaries (some
    # accounts saturate, others idle). The seamless 6h PTS reset (in push_live)
    # ALREADY re-execs ffmpeg at this boundary with a #EXT-X-DISCONTINUITY, so
    # returning to the primary here costs NO extra reconnect — we just point the
    # already-happening restart back at source[0]. Guards: only when steady
    # (LIVE_FAIL==0, so we don't abort an in-progress failover cascade — a
    # standby cycle already resets to primary), only when actually drifted
    # (URL_IDX!=0), mpegts only. If the primary is down at that instant the
    # normal cascade re-selects a working source, absorbed by the player buffer.
    if [ "$SEGMENT_TYPE" != "fmp4" ] && [ "$LIVE_FAIL" -eq 0 ] && [ "$URL_IDX" -ne 0 ] && [ "$NUM_URLS" -gt 1 ]; then
        _drift_now=$(date +%s)
        _drift_start=$(cat "$SESSION_FILE" 2>/dev/null || echo "$_drift_now")
        if [ $(( _drift_now - _drift_start )) -ge "$MAX_SESSION_SECS" ]; then
            log "  DRIFT-RESET: 6h boundary – returning source[$URL_IDX] → primary source[0] (rides the seamless PTS reset; no extra reconnect)"
            URL_IDX=0
        fi
    fi
    SOURCE_URL="${SOURCE_URLS[$URL_IDX]:-}"
    if [[ -n "$SOURCE_URL" ]] && [[ $LIVE_FAIL -lt $MAX_FAILS ]]; then
        # fmp4 standby→live transition: standby's init.mp4 differs from the live
        # encode's, so wipe for a clean manifest+init. live→live token restarts keep
        # LAST_MODE=live and skip this (their init is byte-identical → append_list
        # continues seamlessly).
        # Never append_list across a mode or codec change. mpegts used to keep the
        # old playlist; if the last run published audio-only (or standby SPS then
        # a dead live), hls.js hits bufferAppendError / 0×0 on every channel.
        if [ "$LAST_MODE" = "standby" ]; then
            log "  standby→live – wiping HLS so codecs cannot mix"
            wipe_hls
        elif playlist_is_novideo; then
            log "  wiping HLS – last completed segment has no video"
            wipe_hls
        fi
        LAST_MODE="live"
        T_START=$(date +%s)
        push_live
        EXIT=$?
        T_RUN=$(( $(date +%s) - T_START ))
        if [[ ${FFMPEG_CAP:-0} -gt 0 && $T_RUN -ge $((FFMPEG_CAP - 3)) ]]; then
            # PLANNED exit: ffmpeg reached its -t cap (proactive token refresh, or the 6h
            # session reset). Reconnect to the SAME url for a FRESH token — this is by design,
            # NOT a failure, so it never counts toward failover even when the refresh interval
            # is below HEALTHY_RUN_SECS. This is the whole point: pull a new token cleanly
            # (~5s gap) BEFORE the old one silently dies (which would cost a ~15s watchdog gap).
            LIVE_FAIL=0
            log "Live exited (code=$EXIT) ran=${T_RUN}s on source[$URL_IDX] – planned token refresh, reconnecting"
        elif [[ $T_RUN -ge $HEALTHY_RUN_SECS ]]; then
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
            log "Live unavailable – no-standby; brief hold, retrying live (keep last playlist)"
            sleep 2
        else
            if [ "$LAST_MODE" != "standby" ]; then
                log "  live→standby – wiping HLS so slate cannot mix with live codecs"
                wipe_hls
            fi
            LAST_MODE="standby"
            push_standby || true
            log "Standby ended – resetting to primary, retrying live"
        fi
        LIVE_FAIL=0
        URL_IDX=0   # after a standby/hold cycle, start over from the primary URL
        rm -f "$AUDIO_CACHE" 2>/dev/null   # source may have changed → re-probe audio
    fi
    sleep 0.3
done
