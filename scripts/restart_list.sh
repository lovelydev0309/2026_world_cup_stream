#!/bin/bash
# Restart the named channels so they pick up the run_channel.sh backoff change.
# Wrapper caches its args, so the wrapper must die; relaunch immediately the way
# rpa_watchdog start_channel() does (flock + its duplicate guard make this safe).
cd /opt/streaming-stack
for CH in "$@"; do
  for P in $(ps -eo pid,cmd | grep -E "hls/${CH}/index\.m3u8" | grep -v grep | awk '{print $1}'); do kill -9 "$P" 2>/dev/null; done
  for P in $(ps -eo pid,cmd | grep -E "run_channel\.sh ${CH}( |$)" | grep -v grep | awk '{print $1}'); do kill -9 "$P" 2>/dev/null; done
  sleep 1
  setsid bash /opt/streaming-stack/scripts/run_channel.sh "$CH" </dev/null >>logs/${CH}_stdout.log 2>&1 8>&- &
  echo "[$(date -u +%H:%M:%S)] relaunched $CH"
  sleep 7
done
