#!/usr/bin/env python3
"""Finalize newly-ingested VOD (isolated on vod-disk-us/_majo_new, served at
/player/vod-us/_majo_new/) into the MAJO served catalog: register each completed
movie in vod-disk/_ingest.jsonl flagged featured+new_release, feature the already-
served target titles, bump every featured title's `added` above the whole catalog
so it leads the site's default "newest first" sort, then regen. Idempotent."""
import json,os,re,sys,shutil,time,unicodedata
SPARE="/opt/streaming-stack/vod-disk-us/_majo_new"; DISK="/opt/streaming-stack/vod-disk"
def norm(s):
    s=unicodedata.normalize("NFKD",s or ""); s="".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+"," ",s.lower()).strip()
def loadjson(p,d):
    try: return json.load(open(p))
    except: return d
def completed(d):
    idx=os.path.join(d,"index.m3u8")
    return os.path.exists(idx) and any(f.endswith(".ts") for f in os.listdir(d))
def yr(m):
    try: return int(str(m.get("year") or "0")[:4])
    except: return 0
new_recs=[]
if os.path.exists(SPARE+"/_ingest.jsonl"):
    for line in open(SPARE+"/_ingest.jsonl"):
        line=line.strip()
        if line:
            try: new_recs.append(json.loads(line))
            except: pass
jl=DISK+"/_ingest.jsonl"; by={}
if os.path.exists(jl):
    for line in open(jl):
        line=line.strip()
        if line:
            try: r=json.loads(line); by[r.get("slug")]=r
            except: pass
before=set(by); added=0
for r in new_recs:
    slug=r.get("slug")
    if not slug or not completed(os.path.join(SPARE,slug)): continue
    key=slug
    if key in by and str(by[key].get("stream_id"))!=str(r.get("stream_id")):
        key=f"{slug}-{r.get('stream_id')}"
    rr=dict(r); rr["slug"]=key; rr["featured"]=True; rr["new_release"]=True
    if key not in before: added+=1
    by[key]=rr
orig=loadjson(DISK+"/_original.json",[])
tgt=loadjson(DISK+"/_featured_targets.json",{"sids":[],"titles":[]})
sids={str(x) for x in tgt.get("sids",[])}; titles=set(tgt.get("titles",[]))
def feat(rows):
    n=0
    for m in rows:
        if str(m.get("stream_id")) in sids or norm(m.get("title","")) in titles:
            if not m.get("featured"): n+=1
            m["featured"]=True; m["new_release"]=True
    return n
fj=feat(list(by.values())); fo=feat(orig)
# bump `added` on every featured title above the whole catalog -> leads "newest first" sort
NOW=int(time.time())
feats=[m for m in by.values() if m.get("featured")]+[m for m in orig if m.get("featured")]
feats.sort(key=lambda m:(-yr(m), str(m.get("title","")).lower()))
for i,m in enumerate(feats): m["added"]=NOW+(len(feats)-i)
# featured titles must be visible; hide only exact-title duplicates
shown=set(); allm=list(by.values())+orig
for m in allm:
    if not m.get("hidden") and not m.get("featured"): shown.add(norm(m.get("title","")))
for m in allm:
    if m.get("featured"):
        t=norm(m.get("title",""))
        if t in shown: m["hidden"]=True
        else: m["hidden"]=False; shown.add(t)
# persist
shutil.copy2(jl, jl+".bak-finalize") if os.path.exists(jl) else None
with open(jl+".tmp","w") as f:
    for r in by.values(): f.write(json.dumps(r,ensure_ascii=False)+"\n")
os.replace(jl+".tmp", jl)
shutil.copy2(DISK+"/_original.json", DISK+"/_original.json.bak-finalize")
json.dump(orig, open(DISK+"/_original.json","w"), ensure_ascii=False)
print(f"added={added} featured(jsonl={fj},orig={fo}) total_featured={len(feats)} total_recs={len(by)}")
sys.path.insert(0,"/opt/streaming-stack/scripts")
import vod_ingest2 as m; m.regen_movies_json()
