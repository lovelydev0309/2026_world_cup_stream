#!/usr/bin/env python3
"""Upload the newly-added MAJO releases (on vod-disk-us/_majo_new) to the Telegram
storage channel and append them to config/tg_movies_majo.json so they show in the Mini
App. Muxes HLS->faststart MP4 (copy); a film over Telegram's ~2GB bot limit is
re-encoded to ~1.6GB 1080p first. Small-first ordering; idempotent (skips films already
in the catalog)."""
import os, subprocess, json
from telethon.sync import TelegramClient
from telethon.tl.types import DocumentAttributeVideo
def cfg(k,d=None):
    for line in open("/opt/streaming-stack/config/accounts.env"):
        line=line.strip()
        if line and not line.startswith("#") and "=" in line:
            kk,vv=line.split("=",1)
            if kk.strip()==k: return vv.strip()
    return d
API_ID=int(cfg("TG_API_ID")); API_HASH=cfg("TG_API_HASH")
TOK=cfg("MAJO_BOT_TOKEN"); CHAN=int(cfg("TG_STORAGE_CHANNEL"))
SRC="/opt/streaming-stack/vod-disk-us/_majo_new"
CATALOG="/opt/streaming-stack/config/tg_movies_majo.json"
WEB="/opt/streaming-stack/player/tg-mx/movies-tg.json"
SCRATCH=SRC+"/_tmp"; os.makedirs(SCRATCH,exist_ok=True)
def log(m): print(m,flush=True)
meta={m["slug"]:m for m in json.load(open("/opt/streaming-stack/vod-disk/movies.json")) if m.get("slug")}
new=[]
for l in open(SRC+"/_ingest.jsonl"):
    l=l.strip()
    if l:
        try: new.append(json.loads(l)["slug"])
        except: pass
new=list(dict.fromkeys(new))
existing={e["slug"] for e in json.load(open(CATALOG))} if os.path.exists(CATALOG) else set()
def ts_size(s):
    p=SRC+"/"+s
    try: return sum(os.path.getsize(p+"/"+f) for f in os.listdir(p) if f.endswith(".ts"))
    except: return 0
def dur_of(s):
    d=(meta.get(s,{}).get("duration") or "0:0:0").split(":")
    try:
        d=[int(x) for x in d]
        while len(d)<3: d=[0]+d
        return d[0]*3600+d[1]*60+d[2]
    except: return 0
todo=[s for s in new if s not in existing]
todo.sort(key=ts_size)   # small/fitting first -> quick wins land first
log("to upload: %d (already on TG: %d)"%(len(todo),len(existing)))
def prep(slug):
    src=SRC+"/"+slug+"/index.m3u8"; mp4=SCRATCH+"/"+slug+".mp4"
    if not os.path.exists(src): return None
    sz=ts_size(slug); dur=dur_of(slug)
    if 0<sz<1.9e9:
        cmd=["ffmpeg","-y","-loglevel","error","-i",src,"-c","copy","-bsf:a","aac_adtstoasc","-movflags","+faststart",mp4]
    else:
        vbit=max(900000,int(1.6e9*8/max(dur,1))-128000)
        cmd=["ffmpeg","-y","-loglevel","error","-i",src,"-c:v","libx264","-preset","veryfast",
             "-b:v",str(vbit),"-maxrate",str(int(vbit*1.5)),"-bufsize",str(int(vbit*2)),
             "-vf","scale='min(1920,iw)':-2","-pix_fmt","yuv420p","-threads","2",
             "-c:a","aac","-b:a","128k","-ac","2","-movflags","+faststart",mp4]
    try: subprocess.run(cmd,check=True,timeout=18000)
    except Exception as e: log("  prep-fail %s: %s"%(slug,str(e)[:80])); return None
    if not os.path.exists(mp4): return None
    g=os.path.getsize(mp4)
    if g<5e6 or g>1.98e9:
        log("  bad-size %s %.2fGB"%(slug,g/1e9)); 
        try: os.remove(mp4)
        except: pass
        return None
    return mp4
def probe(mp4):
    try:
        j=json.loads(subprocess.run(["ffprobe","-v","error","-select_streams","v:0","-show_entries","stream=width,height:format=duration","-of","json",mp4],capture_output=True,text=True,timeout=60).stdout)
        st=j["streams"][0]; return int(st["width"]),int(st["height"]),int(float(j["format"]["duration"]))
    except: return 0,0,0
def thumb(mp4,slug):
    th=SCRATCH+"/"+slug+"_th.jpg"
    try:
        subprocess.run(["ffmpeg","-y","-loglevel","error","-ss","120","-i",mp4,"-frames:v","1","-vf","scale=320:-1",th],check=True,timeout=120)
        if os.path.exists(th) and os.path.getsize(th)>1000: return th
    except: pass
    return None
client=TelegramClient("/opt/streaming-stack/config/majo_uploader_tl",API_ID,API_HASH)
client.start(bot_token=TOK)
ent=client.get_entity(CHAN)
catalog=json.load(open(CATALOG)) if os.path.exists(CATALOG) else []
def save():
    tmp=CATALOG+".tmp"; json.dump(catalog,open(tmp,"w"),ensure_ascii=False,indent=1); os.replace(tmp,CATALOG)
    try: json.dump(catalog,open(WEB,"w"),ensure_ascii=False,indent=1)
    except: pass
ok=0
for slug in todo:
    m=meta.get(slug,{})
    mp4=prep(slug)
    if not mp4: continue
    w,h,dur=probe(mp4); attrs=[DocumentAttributeVideo(duration=dur,w=w,h=h,supports_streaming=True)] if w and h else None
    th=thumb(mp4,slug); cap="%s%s"%(m.get("title") or slug,"  (%s)"%m.get("year") if m.get("year") else "")
    try: msg=client.send_file(ent,mp4,caption=cap,supports_streaming=True,attributes=attrs,thumb=th)
    except Exception as e: log("  upload-fail %s: %s"%(slug,str(e)[:120])); msg=None
    for f in (mp4,th):
        try:
            if f: os.remove(f)
        except: pass
    if not msg: continue
    catalog.append({"slug":slug,"title":m.get("title"),"year":m.get("year"),"rating":m.get("rating"),
                    "genre":m.get("genre"),"duration":m.get("duration"),"message_id":msg.id,"new_release":True})
    ok+=1; save(); log("  OK %s -> msg %s (%dMB)"%(slug,msg.id,os.path.getsize(mp4) if os.path.exists(mp4) else 0))
client.disconnect(); log("DONE uploaded=%d catalog_now=%d"%(ok,len(catalog)))
