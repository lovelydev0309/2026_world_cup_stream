#!/bin/bash
# Parallel US subtitle extraction: 3 workers partitioned by stream_id % 3.
# Workers 0,1 -> dedicated VOD account (2 of its 3 slots; download uses the 3rd).
# Worker 2   -> KOZEE2 (1 of its spare slots; live US channels keep failover room).
# Subtitle work is CPU-light (probe + copy sub tracks), so it won't touch the
# live channels' CPU. Runs a pass now, waits for the ingest to finish, runs a
# final pass over the remainder, regenerates movies.json, writes a marker.
US=/opt/streaming-stack/vod-disk-us
cd /opt/streaming-stack/scripts
set -a; source /opt/streaming-stack/config/accounts.env; set +a
K2H="${KOZEE2%%/*}"; K2U=$(echo "$KOZEE2"|cut -d/ -f2); K2P=$(echo "$KOZEE2"|cut -d/ -f3)
export VOD_DISK="$US" VOD_CDN="https://stream.tv247on.com/player/vod-us"
log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$US/orchestrator.log"; }
regen(){ python3 -c "import os,sys;os.environ['VOD_DISK']='$US';os.environ['VOD_CDN']='$VOD_CDN';sys.path.insert(0,'/opt/streaming-stack/scripts');import vod_ingest2 as v;v.regen_movies_json()"; }

run_pass(){   # 3 workers in parallel, wait for all
  WORKER_N=3 WORKER_I=0 python3 add_subs.py >> "$US/subs.out" 2>&1 &
  WORKER_N=3 WORKER_I=1 python3 add_subs.py >> "$US/subs.out" 2>&1 &
  WORKER_N=3 WORKER_I=2 SUB_HOST="$K2H" SUB_USER="$K2U" SUB_PW="$K2P" python3 add_subs.py >> "$US/subs.out" 2>&1 &
  wait
}

log "PARALLEL orchestrator start (3 subtitle workers)"
# wait for any single-worker subs still running from the previous orchestrator
while pgrep -f "add_subs.py$" >/dev/null || pgrep -f "add_subs.py brother" >/dev/null; do sleep 15; done
log "subs pass 1 (parallel, current catalog)"
run_pass
regen
log "pass 1 done + regen"
# wait for the download ingest to finish
while pgrep -f "vod_ingest2.py 300" >/dev/null; do sleep 120; done
log "ingest finished"
log "subs pass 2 (parallel, remaining)"
run_pass
regen
log "pass 2 done + regen"
python3 -c "import json;US='$US';mv=json.load(open(US+'/movies.json'));mv=mv if isinstance(mv,list) else mv.get('movies',[]);subbed=sum(1 for m in mv if m.get('subtitles'));json.dump({'movies':len(mv),'subtitled':subbed,'done':True},open(US+'/_COMPLETE.json','w'))"
log "COMPLETE"
