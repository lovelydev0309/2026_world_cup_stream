import os, re, json, glob, subprocess, time, unicodedata, sys
DISK="/opt/streaming-stack/vod-disk"
HOST="tvon247.com"; USER="2E3VBEM"; PW="QA91PXZ"; UA="okhttp/4.9.3"
LOG=DISK+"/subs.log"
ONLY=set(sys.argv[1:])  # optional slug filter for testing
TEXT={"subrip","srt","ass","ssa","mov_text","webvtt","text","microdvd"}
LANG={"spa":("es","Español"),"es":("es","Español"),"eng":("en","English"),"en":("en","English"),
 "por":("pt","Português"),"pt":("pt","Português"),"fre":("fr","Français"),"fra":("fr","Français"),"fr":("fr","Français"),
 "ger":("de","Deutsch"),"deu":("de","Deutsch"),"de":("de","Deutsch"),"ita":("it","Italiano"),"it":("it","Italiano"),
 "rus":("ru","Русский"),"jpn":("ja","日本語"),"chi":("zh","中文"),"zho":("zh","中文"),"ara":("ar","العربية"),
 "kor":("ko","한국어"),"pol":("pl","Polski"),"tur":("tr","Türkçe"),"hin":("hi","हिन्दी"),"nld":("nl","Nederlands"),
 "dut":("nl","Nederlands"),"swe":("sv","Svenska"),"cze":("cs","Čeština"),"gre":("el","Ελληνικά"),"hrv":("hr","Hrvatski")}
PRIO={"es":0,"en":1,"pt":2}
def log(m): open(LOG,"a").write("[%s] %s\n"%(time.strftime("%H:%M:%S"),m))
def norm(s):
    s=unicodedata.normalize("NFKD",s or "").encode("ascii","ignore").decode()
    return re.sub(r'[^a-z0-9]','',s.lower())
def clean(n):
    n=re.sub(r'^\s*[A-Za-z]{2,4}\s*-\s*','',n or ''); n=re.sub(r'\[[^\]]*\]','',n)
    n=re.sub(r'\s*-\s*(19|20)\d{2}\s*$','',n); return n.strip(' -')

cat=json.load(open(DISK+"/_catalog.json"))
ext_by_id={x["stream_id"]:(x.get("container_extension") or "mp4") for x in cat}
by_title={}
for x in cat: by_title.setdefault(norm(clean(x.get("name",""))), x["stream_id"])
ing={}
for line in open(DISK+"/_ingest.jsonl"):
    try: j=json.loads(line); ing[j["slug"]]=j["stream_id"]
    except: pass
mj={m["slug"]:m.get("title","") for m in json.load(open(DISK+"/movies.json"))}
OVERRIDE={"gorky-resort":1448112}
VID='<video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>'
RECON=["-reconnect","1","-reconnect_streamed","1","-reconnect_on_network_error","1","-reconnect_delay_max","5","-rw_timeout","30000000"]

movies=[]
for p in sorted(glob.glob(DISK+"/*/index.html")):
    slug=p.split("/")[-2]
    if ONLY and slug not in ONLY: continue
    sid=ing.get(slug) or OVERRIDE.get(slug) or by_title.get(norm(mj.get(slug,slug)))
    if not sid: log("SKIP %s: no source id"%slug); continue
    movies.append((slug,sid,ext_by_id.get(sid,"mp4")))

log("=== subs start: %d movies ==="%len(movies))
ok=nosub=skip=fail=0
for slug,sid,ext in movies:
    d=os.path.join(DISK,slug); page=os.path.join(d,"index.html")
    html=open(page,encoding="utf-8").read()
    if 'kind="subtitles"' in html: skip+=1; continue
    url="http://%s/movie/%s/%s/%s.%s"%(HOST,USER,PW,sid,ext)
    try:
        r=subprocess.run(["ffprobe","-v","error","-user_agent",UA]+RECON+["-select_streams","s",
            "-show_entries","stream=index,codec_name:stream_tags=language","-of","json",url],
            capture_output=True,text=True,timeout=120)
        streams=json.loads(r.stdout or "{}").get("streams",[])
    except Exception as e:
        log("PROBE-FAIL %s: %s"%(slug,str(e)[:60])); fail+=1; continue
    picks=[]; seen=set()
    for s in streams:
        if s.get("codec_name") not in TEXT: continue
        raw=((s.get("tags",{}) or {}).get("language","") or "").lower()
        code,label=LANG.get(raw,(raw[:2] or "und", raw.upper() if raw else "Sub"))
        if code in seen: continue
        seen.add(code); picks.append((s["index"],code,label))
    picks.sort(key=lambda t:PRIO.get(t[1],5))
    picks=picks[:6]
    if not picks: log("NO-SUBS %s"%slug); nosub+=1; continue
    cmd=["ffmpeg","-y","-nostdin","-loglevel","error","-user_agent",UA]+RECON+["-i",url]
    outs=[]
    for idx,code,label in picks:
        vtt=os.path.join(d,"sub_%s.vtt"%code); cmd+=["-map","0:%d"%idx,"-c:s","webvtt",vtt]
        outs.append((vtt,code,label))
    t0=time.time()
    try: subprocess.run(cmd,capture_output=True,text=True,timeout=3600)
    except Exception as e: log("EXTRACT-ERR %s: %s"%(slug,str(e)[:50]))
    good=[(v,c,l) for v,c,l in outs if os.path.exists(v) and os.path.getsize(v)>60]
    if not good: log("EXTRACT-EMPTY %s"%slug); fail+=1; continue
    tracks="".join('<track kind="subtitles" src="%s" srclang="%s" label="%s">'%(os.path.basename(v),c,l) for v,c,l in good)
    nh=html.replace(VID,'<video id="v" controls playsinline poster="poster.jpg" preload="metadata">'+tracks+'</video>',1)
    if nh!=html:
        open(page,"w",encoding="utf-8").write(nh); ok+=1
        log("OK %s: %s (%.0fs)"%(slug,",".join(c for _,c,_ in good),time.time()-t0))
    else:
        log("INJECT-FAIL %s (video tag not found)"%slug); fail+=1
log("=== DONE ok=%d nosub=%d skip=%d fail=%d ==="%(ok,nosub,skip,fail))
