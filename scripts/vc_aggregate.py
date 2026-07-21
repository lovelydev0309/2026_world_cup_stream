#!/usr/bin/env python3
# View-count aggregator: tally new /vc beacon hits from the nginx beacon log into
# _views.json (per-movie play counts), then regenerate movies.json so views + the
# most-viewed tag reflect real data. Byte-cursor so each line is counted once.
import json, os, sys
DISK="/opt/streaming-stack/vod-disk"
LOG="/opt/streaming-stack/vc-logs/beacon.log"
VIEWS=DISK+"/_views.json"
CACHE="/opt/streaming-stack/cache"; os.makedirs(CACHE,exist_ok=True)
CURSOR=CACHE+"/vc_cursor"
try: off=int(open(CURSOR).read().strip())
except Exception: off=0
try: size=os.path.getsize(LOG)
except Exception: size=0
if size<off: off=0   # log truncated/rotated -> restart
new=""
if size>off:
    with open(LOG,encoding="utf-8",errors="ignore") as f:
        f.seek(off); new=f.read(); off=f.tell()
counts={}
for line in new.splitlines():
    p=line.split()
    if len(p)>=2:
        slug=p[1].strip()
        if slug and slug!="-": counts[slug]=counts.get(slug,0)+1
views={}
try: views=json.load(open(VIEWS))
except Exception: pass
changed=False
for slug,n in counts.items():
    if os.path.isdir(os.path.join(DISK,slug)):   # ignore junk/unknown slugs
        views[slug]=int(views.get(slug,0))+n; changed=True
if changed:
    json.dump(views, open(VIEWS,"w"))
# truncate the beacon log if it grows large (nginx re-appends from 0)
if off>10*1024*1024:
    try: open(LOG,"w").close(); off=0
    except Exception: pass
open(CURSOR,"w").write(str(off))
if changed:
    sys.path.insert(0,"/opt/streaming-stack/scripts")
    import vod_ingest2 as v; v.regen_movies_json()
