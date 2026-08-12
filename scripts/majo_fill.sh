#!/bin/bash
# Backfill the MAJO VOD disk with acclaimed high-IMDb films, end to end:
#   reconcile ingest state -> ingest (hidden) -> real IMDb ratings -> genres -> curate.
# Throttled (nice/ionice) so it never disturbs live TV. Run in background:
#   nohup bash scripts/majo_fill.sh > logs/majo_fill.log 2>&1 &
cd /opt/streaming-stack || exit 1
echo "=== FILL START $(date -u +%FT%TZ) ==="

# 1) Reconcile ingest state so regen_movies_json() can't resurrect the pruned films:
#    base catalog = the 143 kept; clear the old ingest log (held the 192 deleted).
cp vod-disk/movies.json vod-disk/_original.json
: > vod-disk/_ingest.jsonl
echo "state reconciled: base=$(python3 -c "import json;print(len(json.load(open('vod-disk/_original.json'))))") films, ingest log cleared"

# 2) Ingest the acclaimed titles (hidden until rated), stop if disk free < MIN_FREE GB.
#    INGEST_TRANSCODE=1 recovers HEVC/non-AAC too (slower). Configurable via env.
VOD_RESTORE=1 VOD_RESTORE_FILE="${IDS_FILE:-config/majo_ingest_ids.txt}" INGEST_HIDDEN=1 \
  INGEST_TRANSCODE="${INGEST_TRANSCODE:-0}" \
  nice -n 19 ionice -c3 python3 scripts/vod_ingest2.py "${INGEST_TARGET:-130}" "${MIN_FREE:-40}"
echo "=== ingest phase done $(date -u +%FT%TZ) ==="

# 3) Real IMDb ratings for the NEW films only (skip the already-rated 143)
ONLY_UNRATED=1 python3 scripts/omdb_rerate.py /opt/streaming-stack/vod-disk/movies.json

# 4) Clean Spanish genre tags + curate (show real-rated >=5.5, hide unrated/low/truncated)
python3 scripts/vod_normalize_genres.py /opt/streaming-stack/vod-disk/movies.json
python3 scripts/vod_curate.py /opt/streaming-stack/vod-disk/movies.json 5.5

# 5) Publish: served movies.json = only films that are NOT hidden AND actually playable
#    (video+poster verified on disk). Bulletproof against stale flags so the majotv.com
#    WordPress plugin (which doesn't filter hidden) never shows broken/low-quality films.
python3 scripts/majo_publish.py

SHOWN=$(python3 -c "import json;d=json.load(open('vod-disk/movies.json'));print(len([m for m in d if m.get('m3u8_url') and not m.get('hidden')]))")
TOTAL=$(python3 -c "import json;print(len(json.load(open('vod-disk/movies.json'))))")
echo "=== FILL DONE $(date -u +%FT%TZ): catalog total=$TOTAL, shown=$SHOWN ==="