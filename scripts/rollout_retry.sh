#!/bin/bash
# Apply the retry-discipline change to every live channel. The wrapper caches its ffmpeg
# args, so the WRAPPER must be replaced; relaunch immediately the way rpa_watchdog's
# start_channel() does (flock + its duplicate guard make the race safe), 8s apart so only
# one channel is briefly out at a time.
cd /opt/streaming-stack
LOG=logs/rollout_retry.log; : > "$LOG"
mapfile -t CHANS < <(python3 -c "
import json,re
d=json.load(open('player/channels.json'))
print('\n'.join(sorted((c['name'] for c in d), key=lambda n:int(re.sub(r'\D','',n)))))")
for CH in "${CHANS[@]}"; do
  for P in $(ps -eo pid,cmd | grep -E "hls/${CH}/index\.m3u8" | grep -v grep | awk '{print $1}'); do kill -9 "$P" 2>/dev/null; done
  for P in $(ps -eo pid,cmd | grep -E "run_channel\.sh ${CH}( |$)" | grep -v grep | awk '{print $1}'); do kill -9 "$P" 2>/dev/null; done
  sleep 1
  docker exec nginx-rtmp sh -c "mkdir -p /var/www/hls/$CH && chmod 1777 /var/www/hls/$CH" 2>/dev/null
  setsid bash /opt/streaming-stack/scripts/run_channel.sh "$CH" </dev/null >>"logs/${CH}_stdout.log" 2>&1 &
  echo "[$(date -u +%H:%M:%S)] relaunched $CH" >> "$LOG"
  sleep 8
done
echo "[$(date -u +%H:%M:%S)] DONE $(grep -c relaunched "$LOG")" >> "$LOG"
