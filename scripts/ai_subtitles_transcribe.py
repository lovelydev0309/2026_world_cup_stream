import os, subprocess, sys, time
from faster_whisper import WhisperModel
DISK="/opt/streaming-stack/vod-disk"
LOG=DISK+"/ai_subs.log"
MOVIES=sys.argv[1:] or ["miguel-angel-blanco-las-48-horas-que-lo-cambiaron-todo",
                        "aquel-verano-en-paris","las-paredes-hablan"]
def log(m): open(LOG,"a").write("[%s] %s\n"%(time.strftime("%H:%M:%S"),m))
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
VID='<video id="v" controls playsinline poster="poster.jpg" preload="metadata"></video>'

log("=== AI subs start: %s ==="%", ".join(MOVIES))
log("loading model 'small' (int8, cpu)...")
model=WhisperModel("small", device="cpu", compute_type="int8", cpu_threads=6)
log("model ready")
for slug in MOVIES:
    d=os.path.join(DISK,slug); page=d+"/index.html"
    if not os.path.exists(page): log("skip %s: no page"%slug); continue
    html=open(page,encoding="utf-8").read()
    if 'kind="subtitles"' in html: log("skip %s: already has subs"%slug); continue
    wav="/tmp/%s.wav"%slug
    log("[%s] extracting audio from HLS..."%slug)
    try:
        subprocess.run(["ffmpeg","-y","-nostdin","-loglevel","error","-i",d+"/index.m3u8",
            "-vn","-ac","1","-ar","16000","-c:a","pcm_s16le",wav],timeout=1800,check=True)
    except Exception as e:
        log("  audio-extract FAIL: %s"%str(e)[:80]); continue
    # Spanish transcription
    t0=time.time()
    segs,info=model.transcribe(wav,language="es",vad_filter=True,beam_size=5)
    open(d+"/sub_es.vtt","w",encoding="utf-8").write(to_vtt(segs))
    log("  es transcription done (%.0fs)"%(time.time()-t0))
    # English translation
    t0=time.time()
    segs2,_=model.transcribe(wav,task="translate",vad_filter=True,beam_size=5)
    open(d+"/sub_en.vtt","w",encoding="utf-8").write(to_vtt(segs2))
    log("  en translation done (%.0fs)"%(time.time()-t0))
    try: os.remove(wav)
    except: pass
    tracks=('<track kind="subtitles" src="sub_es.vtt" srclang="es" label="Español (IA)">'
            '<track kind="subtitles" src="sub_en.vtt" srclang="en" label="English (AI)">')
    nh=html.replace(VID,'<video id="v" controls playsinline poster="poster.jpg" preload="metadata">'+tracks+'</video>',1)
    if nh!=html:
        open(page,"w",encoding="utf-8").write(nh); log("  OK %s injected"%slug)
    else:
        log("  INJECT-FAIL %s (video tag not matched)"%slug)
log("=== ALL DONE ===")
