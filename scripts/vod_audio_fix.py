import glob, subprocess, os, time
DISK="/opt/streaming-stack/vod-disk"
LOG=DISK+"/audiofix.log"
def log(m): open(LOG,"a").write("[%s] %s\n"%(time.strftime("%H:%M:%S"),m))
def a_channels(src):
    try:
        r=subprocess.run(["ffprobe","-v","error","-select_streams","a:0","-show_entries","stream=channels","-of","csv=p=0",src],
                         capture_output=True,text=True,timeout=30)
        v=[x for x in (r.stdout or "").split() if x.strip().isdigit()]
        return int(v[0]) if v else None
    except: return None
targets=[]
for m3u8 in sorted(glob.glob(DISK+"/*/index.m3u8")):
    slug=m3u8.split("/")[-2]
    segs=sorted(glob.glob(DISK+"/"+slug+"/seg_*.ts"))
    src=segs[1] if len(segs)>1 else m3u8
    ch=a_channels(src)
    if ch and ch>2: targets.append(slug)
log("=== audio fix: %d movies with >2 audio channels ==="%len(targets))
done=0
for slug in targets:
    d=os.path.join(DISK,slug); tmp=os.path.join(d,"_af")
    os.makedirs(tmp,exist_ok=True)
    for f in glob.glob(tmp+"/*"): os.remove(f)
    cmd=["ffmpeg","-y","-nostdin","-loglevel","error","-i",d+"/index.m3u8",
         "-map","0:v:0","-map","0:a:0","-c:v","copy","-c:a","aac","-ac","2","-b:a","192k","-ar","48000",
         "-f","hls","-hls_time","10","-hls_playlist_type","vod","-hls_flags","independent_segments",
         "-hls_segment_filename",tmp+"/seg_%04d.ts",tmp+"/index.m3u8"]
    t0=time.time()
    try: r=subprocess.run(cmd,capture_output=True,text=True,timeout=2400)
    except Exception as e:
        log("FAIL %s: %s"%(slug,str(e)[:50])); continue
    nseg=sorted(glob.glob(tmp+"/seg_*.ts"))
    nch=a_channels(nseg[1]) if len(nseg)>1 else (a_channels(nseg[0]) if nseg else None)
    if r.returncode!=0 or nch!=2 or not os.path.exists(tmp+"/index.m3u8"):
        log("FAIL %s rc=%s ch=%s"%(slug,r.returncode,nch))
        for f in glob.glob(tmp+"/*"): os.remove(f)
        try: os.rmdir(tmp)
        except: pass
        continue
    for f in glob.glob(d+"/seg_*.ts"): os.remove(f)
    os.remove(d+"/index.m3u8")
    for f in glob.glob(tmp+"/*"): os.rename(f, os.path.join(d,os.path.basename(f)))
    os.rmdir(tmp)
    done+=1
    log("OK %s -> stereo (%.0fs) [%d/%d]"%(slug,time.time()-t0,done,len(targets)))
log("=== audio fix DONE: %d fixed ==="%done)
