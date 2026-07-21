#!/usr/bin/env python3
# Parallel AI-subtitle worker. Run several copies (arg = worker id). Each claims
# an un-subtitled movie via an atomic lock dir, AUTO-DETECTS its language (fixes the
# old forced-Spanish mislabel bug), writes source-language subs + an English
# translation (when source isn't English), injects <track> tags, marks .ai_langs.
# Loops until no un-subtitled movies remain.
import os, subprocess, sys, time, glob, random
from faster_whisper import WhisperModel
DISK="/opt/streaming-stack/vod-disk"
LOG=DISK+"/ai_subs.log"
WK=sys.argv[1] if len(sys.argv)>1 else "0"
LABEL={"es":"Español","en":"English","pt":"Português","fr":"Français","it":"Italiano","de":"Deutsch",
 "ru":"Русский","ja":"日本語","zh":"中文","ko":"한국어","ar":"العربية","pl":"Polski","nl":"Nederlands",
 "sv":"Svenska","tr":"Türkçe","hi":"हिन्दी","ca":"Català","gl":"Galego","eu":"Euskara","ro":"Română"}
VID='<video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>'
def log(m): open(LOG,"a").write("[%s w%s] %s\n"%(time.strftime("%m-%d %H:%M:%S"),WK,m))
def fmt(t):
    if t<0:t=0
    h=int(t//3600);mn=int((t%3600)//60);s=t-h*3600-mn*60
    return "%02d:%02d:%06.3f"%(h,mn,s)
def to_vtt(segs):
    o=["WEBVTT",""]
    for s in segs:
        x=(s.text or "").strip()
        if x: o+=["%s --> %s"%(fmt(s.start),fmt(s.end)),x,""]
    return "\n".join(o)+"\n"
def regen():
    try: subprocess.run(["python3","-c","import sys;sys.path.insert(0,'/opt/streaming-stack/scripts');import vod_ingest2 as v;v.regen_movies_json()"],timeout=90)
    except Exception: pass
def available():
    out=[]
    for page in glob.glob(DISK+"/*/index.html"):
        d=os.path.dirname(page)
        if os.path.exists(d+"/_ailock") or os.path.exists(d+"/_aifail"): continue
        try:
            if 'kind="subtitles"' in open(page,encoding="utf-8").read(): continue
        except Exception: continue
        out.append((os.path.basename(d),d,page))
    return out

model=WhisperModel("small",device="cpu",compute_type="int8",cpu_threads=4)
log("worker ready")
n=0; idle=0
while True:
    av=available()
    if not av:
        idle+=1
        if idle>=3: log("no work left, exiting (did %d)"%n); break
        time.sleep(20); continue
    idle=0; random.shuffle(av); claim=None
    for slug,d,page in av:
        try: os.mkdir(d+"/_ailock"); claim=(slug,d,page); break
        except FileExistsError: continue
    if not claim: time.sleep(3); continue
    slug,d,page=claim; t0=time.time()
    try:
        wav="/tmp/w%s.wav"%WK
        subprocess.run(["ffmpeg","-y","-nostdin","-loglevel","error","-i",d+"/index.m3u8",
            "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",wav],timeout=1800,check=True)
        segs,info=model.transcribe(wav,vad_filter=True,beam_size=5)   # AUTO-DETECT language
        src=(info.language or "es").lower()
        open(d+"/sub_%s.vtt"%src,"w",encoding="utf-8").write(to_vtt(segs))
        langs=[src]
        if src!="en":
            segs2,_=model.transcribe(wav,task="translate",vad_filter=True,beam_size=5)  # -> English
            open(d+"/sub_en.vtt","w",encoding="utf-8").write(to_vtt(segs2)); langs.append("en")
        try: os.remove(wav)
        except: pass
        html=open(page,encoding="utf-8").read()
        tracks="".join('<track kind="subtitles" src="sub_%s.vtt" srclang="%s" label="%s (IA)">'%(l,l,LABEL.get(l,l.upper())) for l in langs)
        nh=html.replace(VID,'<video id="v" controls playsinline poster="poster.jpg" preload="metadata">'+tracks+'</video>',1)
        if nh!=html:
            open(page,"w",encoding="utf-8").write(nh)
            open(d+"/.ai_langs","w").write("\n".join(langs)+"\n")
            n+=1; log("OK %s [%s] %.0fs"%(slug,",".join(langs),time.time()-t0))
            if n%4==0: regen()
        else:
            log("INJECT-FAIL %s"%slug); open(d+"/_aifail","w").write("no video tag")
    except Exception as e:
        log("FAIL %s: %s"%(slug,str(e)[:70])); open(d+"/_aifail","w").write(str(e)[:100])
    finally:
        try: os.rmdir(d+"/_ailock")
        except: pass
regen()
