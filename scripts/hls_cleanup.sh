#!/usr/bin/env bash
# Reclaim disk: delete orphaned live HLS segments older than 10 min (never referenced
# by the live playlist, which only holds ~1 min). Segments accumulate across ffmpeg
# restarts because hls_flags delete_segments does not track orphans from prior runs,
# so over days of uptime the hls/ dir can grow to tens of GB and fill the disk —
# which presents identically to a provider outage (channels flap/drop as segment
# writes fail). Installed as cron: */15 * * * * /opt/streaming-stack/scripts/hls_cleanup.sh
for d in /opt/streaming-stack/hls/channel*; do
  find "$d" -maxdepth 1 \( -name "*.ts" -o -name "*.m4s" \) -mmin +10 -delete 2>/dev/null
done
# cap unbounded ffmpeg stdout logs at 50MB
for f in /opt/streaming-stack/logs/*_stdout.log; do
  sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
  [ "$sz" -gt 52428800 ] && : > "$f"
done
