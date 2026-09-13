#!/bin/bash
# Roll the new -maxrate 1400k to every channel that is still on the old cap.
# Kill the wrapper (it caches its args) AND its ffmpeg child, then relaunch
# immediately the same way rpa_watchdog.sh start_channel() does, so the gap is
# ~5-10s instead of the 15-176s that waiting for RPA's DEAD detection costs.
# run_channel.sh holds a flock and RPA's start_channel() refuses to stack a
# second script, so a concurrent RPA restart cannot duplicate the producer.
cd /opt/streaming-stack
LOG=logs/rollout_bitrate.log
SCRIPT_DIR=/opt/streaming-stack/scripts
LOG_DIR=/opt/streaming-stack/logs
: > "$LOG"

mapfile -t CHANS < <(python3 -c "
import json
d=json.load(open('player/channels.json'))
print('\n'.join(sorted((c['name'] for c in d), key=lambda n:int(''.join(filter(str.isdigit,n))))))")

done_n=0; skip_n=0
for CH in "${CHANS[@]}"; do
  if ps -eo cmd | grep -E "hls/${CH}/index\.m3u8" | grep -v grep | grep -q "maxrate 1400k"; then
    echo "[$(date +%H:%M:%S)] $CH already on 1400k - skip" >> "$LOG"; skip_n=$((skip_n+1)); continue
  fi
  FP=$(ps -eo pid,cmd | grep -E "hls/${CH}/index\.m3u8" | grep -v grep | awk '{print $1}')
  WP=$(ps -eo pid,cmd | grep -E "run_channel\.sh ${CH}( |$)" | grep -v grep | awk '{print $1}')
  [ -n "$FP" ] && kill -9 $FP 2>/dev/null
  [ -n "$WP" ] && kill -9 $WP 2>/dev/null
  sleep 1
  docker exec nginx-rtmp sh -c "mkdir -p /var/www/hls/$CH && chmod 1777 /var/www/hls/$CH" 2>/dev/null || true
  setsid bash "$SCRIPT_DIR/run_channel.sh" "$CH" </dev/null >>"$LOG_DIR/${CH}_stdout.log" 2>&1 8>&- &
  echo "[$(date +%H:%M:%S)] $CH relaunched" >> "$LOG"
  done_n=$((done_n+1))
  sleep 9
done
echo "[$(date +%H:%M:%S)] DONE relaunched=$done_n skipped=$skip_n" >> "$LOG"
