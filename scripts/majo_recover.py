#!/usr/bin/env python3
"""
Re-pull films that HAVE a real IMDb rating >= THRESH but are missing their video on disk
(e.g. Lilo & Stitch, Is God Is — they had catalog entries but the original download failed).
Pulls each into its EXISTING slug dir (so the real rating/metadata is preserved), copying
H.264/AAC or transcoding otherwise, then generates a poster. Throttled (nice/ionice); stops
if disk free < MIN_FREE. Run majo_publish.py afterwards to serve them.
"""
import json, os, subprocess, urllib.request, time

DISK = "/opt/streaming-stack/vod-disk"
def cfg(k, d=""):
    for l in open("/opt/streaming-stack/config/accounts.env"):
        l = l.strip()
        if l and not l.startswith("#") and "=" in l:
            kk, vv = l.split("=", 1)
            if kk.strip() == k: return vv.strip()
    return d
HOST = cfg("VOD_HOST", "tvon247.com"); USER = cfg("VOD_USER"); PW = cfg("VOD_PW"); UA = "okhttp/4.9.3"
if HOST.startswith("http"): HOST = HOST.split("://", 1)[1]
HOST = HOST.rstrip("/")
THRESH = float(os.environ.get("THRESH", "5.5"))
MIN_FREE = int(os.environ.get("MIN_FREE", "30"))

def free_gb():
    st = os.statvfs(DISK); return st.f_bavail * st.f_frsize / 1e9
def has_video(s):
    p = os.path.join(DISK, s or "")
    try: return os.path.isdir(p) and os.path.exists(p + "/index.m3u8") and any(f.endswith(".ts") for f in os.listdir(p))
    except Exception: return False

cat = json.load(open(DISK + "/_catalog.json")) if os.path.exists(DISK + "/_catalog.json") else []
by_id = {int(c["stream_id"]): c for c in cat if str(c.get("stream_id", "")).isdigit()}
allf = json.load(open(DISK + "/movies_all.json"))
targets = [m for m in allf if isinstance(m.get("rating"), (int, float)) and m["rating"] >= THRESH
           and str(m.get("stream_id", "")).isdigit() and not has_video(m.get("slug", ""))]
print("to recover (rating>=%.1f, no video): %d" % (THRESH, len(targets)),
      [m.get("slug") for m in targets], flush=True)

ok = 0
for m in targets:
    if free_gb() < MIN_FREE:
        print("stop: disk free %.0fGB < %d" % (free_gb(), MIN_FREE), flush=True); break
    slug = m["slug"]; sid = int(m["stream_id"]); c = by_id.get(sid, {})
    if sid not in by_id:
        print("  skip %s: stream_id %s not in provider catalog" % (slug, sid), flush=True); continue
    ext = (c.get("container_extension") or "mp4").lower()
    out = os.path.join(DISK, slug); os.makedirs(out, exist_ok=True)
    url = "http://%s/movie/%s/%s/%s.%s" % (HOST, USER, PW, sid, ext)
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-user_agent", UA, "-select_streams", "v:0",
                             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
                             "-analyzeduration", "6M", "-probesize", "6M", url],
                            capture_output=True, text=True, timeout=60)
        v = (pr.stdout or "").strip().splitlines(); v = v[0] if v else ""
    except Exception:
        v = ""
    if not v:
        print("  skip %s: probe-fail" % slug, flush=True); subprocess.run(["rm", "-rf", out]); continue
    tv = v != "h264"    # transcode any non-h264 video; audio always -> aac
    # disk is tight -> cap 720p, crf 23 keeps transcodes ~1-2GB (not the 8-10GB some hit at 1080/crf21)
    vopts = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
              "-vf", "scale='min(1280,iw)':-2", "-threads", "3"] if tv
             else ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"])
    cmd = ["nice", "-n", "19", "ionice", "-c3", "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
           "-user_agent", UA, "-rw_timeout", "60000000", "-i", url, "-map", "0:v:0", "-map", "0:a:0"] + vopts + [
           "-c:a", "aac", "-ac", "2", "-b:a", "160k", "-ar", "48000",
           "-f", "hls", "-hls_time", "10", "-hls_playlist_type", "vod", "-hls_flags", "independent_segments",
           "-hls_segment_filename", os.path.join(out, "seg_%04d.ts"), os.path.join(out, "index.m3u8")]
    print("  pull %s [%s%s] sid=%s" % (slug, v, "->h264" if tv else " copy", sid), flush=True)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=(9000 if tv else 3600))
    except Exception as e:
        print("  FAIL %s: %s" % (slug, str(e)[:60]), flush=True); subprocess.run(["rm", "-rf", out]); continue
    if r.returncode != 0 or not has_video(slug):
        print("  FAIL %s rc=%s %s" % (slug, r.returncode, (r.stderr or "")[-120:]), flush=True)
        subprocess.run(["rm", "-rf", out]); continue
    # poster: provider art, else a frame
    try:
        info = json.load(urllib.request.urlopen(urllib.request.Request(
            "http://%s/player_api.php?username=%s&password=%s&action=get_vod_info&vod_id=%s" % (HOST, USER, PW, sid),
            headers={"User-Agent": UA}), timeout=30)).get("info", {})
        icon = info.get("movie_image") or info.get("cover_big") or c.get("stream_icon")
        if icon:
            data = urllib.request.urlopen(urllib.request.Request(icon, headers={"User-Agent": UA}), timeout=30).read()
            if len(data) > 2000: open(out + "/poster.jpg", "wb").write(data)
    except Exception:
        pass
    if not os.path.exists(out + "/poster.jpg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "180", "-i", out + "/index.m3u8",
                        "-frames:v", "1", "-vf", "scale=400:-1", out + "/poster.jpg"], timeout=120)
    ok += 1; print("  OK %s%s" % (slug, "  (transcoded)" if tv else ""), flush=True)
    time.sleep(2)
print("RECOVER DONE: %d/%d recovered" % (ok, len(targets)), flush=True)
