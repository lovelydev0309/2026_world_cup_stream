#!/usr/bin/env bash
# Auto-recover channels stuck outputting VIDEO-ONLY (no audio track). Some tvon247
# feeds intermittently drop their audio PID; ffmpeg locks stream selection at connect,
# so if it connects during an audio-less moment the whole run is silent (video plays,
# no audio) — the "channel plays without audio" report. A reconnect re-rolls and almost
# always catches audio. This detects the stuck state from completed output segments and
# restarts the producer. Installed as cron: */4 * * * * .../audio_guard.sh
# NOTE: distinct from the silent-stereo fallback (detect_audio_ok), which still emits a
# 2ch AAC track (shows channels=2); this only fires on a truly absent audio stream.
LOG=/opt/streaming-stack/logs/audio_guard.log
for i in $(seq 1 15); do
  d=/opt/streaming-stack/hls/channel$i
  # inspect 2nd..4th newest segments (skip newest = may be mid-write)
  segs=$(ls -t "$d"/*.ts 2>/dev/null | sed -n '2,4p')
  [ -z "$segs" ] && continue
  noaudio=0; valid=0
  for f in $segs; do
    v=$(ffprobe -v error -select_streams v -show_entries stream=codec_name -of csv=p=0 "$f" 2>/dev/null | head -1)
    a=$(ffprobe -v error -select_streams a -show_entries stream=channels -of csv=p=0 "$f" 2>/dev/null | head -1)
    [ -z "$v" ] && continue          # not a valid video segment, skip
    valid=$((valid+1))
    [ -z "$a" ] && noaudio=$((noaudio+1))
  done
  # restart only if 2+ valid completed segments and ALL are video-only
  if [ "$valid" -ge 2 ] && [ "$noaudio" -eq "$valid" ]; then
    echo "[$(date -u +%FT%TZ)] ch$i VIDEO-ONLY ($noaudio/$valid segs no audio) -> restart" >> "$LOG"
    pkill -f "run_channel.sh channel$i" 2>/dev/null
    pkill -f "hls/channel$i" 2>/dev/null
  fi
done
