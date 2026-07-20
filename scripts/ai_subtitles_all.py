import os, subprocess, sys, time, glob
from faster_whisper import WhisperModel
DISK="/opt/streaming-stack/vod-disk"
LOG=DISK+"/ai_subs.log"
def log(m): open(LOG,"a").write("[%s] %s\n"%(time.strftime("%m-%d %H:%M:%S"),m))
def fmt(t):
    if t<0: t=0
    h=int(t//3600); m=int((t%3600)//60); s=t-h*3600-m*60
    return "%02d:%02d:%06.3f"%(h,m,s)
def to_vtt(segs):
    out=["WEBVTT",""]
    for s in segs:
        txt=(s.text or "").strip()
        if not txt: continue
        out.append("%s --> %s"%(fmt(s.start),fmt(s.end))); out.append(txt); out.append("")
    return "\n".join(out)+"\n"
def regen():
    try: subprocess.run(["python3","-c","import sys;sys.path.insert(0,'/opt/streaming-stack/scripts');import vod_ingest2 as v;v.regen_movies_json()"],timeout=60)
    except Exception as e: log("regen-warn: %s"%e)
VID='<video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>'

# all movies WITHOUT any subtitle track yet
todo=[]
for page in sorted(glob.glob(DISK+"/*/index.html")):
    slug=page.split("/")[-2]
    if 'kind="subtitles"' not in open(page,encoding="utf-8").read(): todo.append(slug)
log("=== FULL AI rollout start: %d films to process ==="%len(todo))
model=WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=4)  # conservative for long unattended run
log("model ready")
done=0
for slug in todo:
    d=os.path.join(DISK,slug); page=d+"/index.html"
    html=open(page,encoding="utf-8").read()
    if 'kind="subtitles"' in html: continue
    wav="/tmp/%s.wav"%slug
    try:
        subprocess.run(["ffmpeg","-y","-nostdin","-loglevel","error","-i",d+"/index.m3u8",
            "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",wav],timeout=1800,check=True)
    except Exception as e:
        log("SKIP %s audio-extract fail: %s"%(slug,str(e)[:60])); continue
    t0=time.time()
    try:
        segs,_=model.transcribe(wav,language="es",vad_filter=True,beam_size=5)
        open(d+"/sub_es.vtt","w",encoding="utf-8").write(to_vtt(segs))
        segs2,_=model.transcribe(wav,task="translate",vad_filter=True,beam_size=5)
        open(d+"/sub_en.vtt","w",encoding="utf-8").write(to_vtt(segs2))
    except Exception as e:
        log("SKIP %s transcribe fail: %s"%(slug,str(e)[:60]))
        try: os.remove(wav)
        except: pass
        continue
    try: os.remove(wav)
    except: pass
    open(d+"/.ai_langs","w").write("es\nen\n")
    tracks=('<track kind="subtitles" src="sub_es.vtt" srclang="es" label="Español (IA)">'
            '<track kind="subtitles" src="sub_en.vtt" srclang="en" label="English (AI)">')
    nh=html.replace(VID,'<video id="v" controls playsinline poster="poster.jpg" preload="metadata">'+tracks+'</video>',1)
    if nh!=html: open(page,"w",encoding="utf-8").write(nh)
    done+=1
    log("OK %s (%.0fs) [%d/%d]"%(slug,time.time()-t0,done,len(todo)))
    if done%3==0: regen()
regen()
log("=== FULL AI rollout DONE: %d films ==="%done)
