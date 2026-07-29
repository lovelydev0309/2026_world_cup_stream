#!/bin/bash
# Orchestrates US catalog subtitle extraction around the running ingest, then
# writes a completion marker. Safe on the 3-slot VOD account (subs=1 conn, and
# it never runs concurrently with itself; ingest=1 conn -> max 2/3).
US=/opt/streaming-stack/vod-disk-us
cd /opt/streaming-stack/scripts
export VOD_DISK="$US"
export VOD_CDN="https://stream.tv247on.com/player/vod-us"
log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$US/orchestrator.log"; }
regen(){ python3 -c "import os,sys;os.environ['VOD_DISK']='$US';os.environ['VOD_CDN']='$VOD_CDN';sys.path.insert(0,'/opt/streaming-stack/scripts');import vod_ingest2 as v;v.regen_movies_json()"; }

log "orchestrator start"
# 1) wait for any in-flight test subs run to finish
while pgrep -f "add_subs.py brother" >/dev/null; do sleep 20; done
# 2) subtitle pass over everything downloaded so far
log "subs pass 1 (current catalog)"
python3 add_subs.py >> "$US/subs.out" 2>&1
regen
log "pass 1 done + regen"
# 3) wait for the ingest to finish (target 300, or candidates/disk exhausted)
while pgrep -f "vod_ingest2.py 300" >/dev/null; do sleep 120; done
log "ingest finished"
# 4) final subtitle pass to cover titles that landed after pass 1
log "subs pass 2 (remaining)"
python3 add_subs.py >> "$US/subs.out" 2>&1
regen
log "pass 2 done + regen"
# 5) completion marker (polled over SSH; counts only, no secrets)
python3 -c "import json;US='$US';mv=json.load(open(US+'/movies.json'));mv=mv if isinstance(mv,list) else mv.get('movies',[]);subbed=sum(1 for m in mv if m.get('subtitles'));json.dump({'movies':len(mv),'subtitled':subbed,'done':True},open(US+'/_COMPLETE.json','w'))"
log "COMPLETE"
