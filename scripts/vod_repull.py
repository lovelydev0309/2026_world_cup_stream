#!/usr/bin/env python3
"""
Re-pull TRUNCATED VOD movies robustly: DOWNLOAD the full source file first, then
remux the LOCAL file to HLS. The provider delivers the whole file to a plain byte
download, but ffmpeg's on-the-fly stream copy chokes on a mid-file glitch and EOFs
early — which is what truncated ~110 movies. Downloading first avoids that; a
transcode fallback gets past any glitch a copy still trips on.

Reads stream_ids from vod-disk-us/_repull.ids. Does NOT touch movies.json.
Usage:  nohup nice -n 15 python3 scripts/vod_repull.py > logs/repull.log 2>&1 &
After it finishes: vod_curate.py (un-hides now-complete titles) + vod_pages_en.py.
"""
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, "/opt/streaming-stack/scripts")
import vod_ingest2 as vi

DISK = "/opt/streaming-stack/vod-disk-us"
UA = vi.UA
ids = [int(x) for x in open(DISK + "/_repull.ids") if x.strip().isdigit()]
mv = {int(m.get("stream_id", 0)): m for m in json.load(open(DISK + "/movies.json"))}
cat = {int(c.get("stream_id", 0)): c for c in vi.api("get_vod_streams")}

def log(m): print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), m), flush=True)
def rm(p):
    try: os.remove(p)
    except Exception: pass

def ffprobe1(path, entries, stream=False):
    sel = ["-select_streams", "v:0"] if stream else []
    r = subprocess.run(["ffprobe", "-v", "error"] + sel + ["-show_entries", entries, "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return (r.stdout or "").strip()

def hls_minutes(mp):
    a = 0.0
    try:
        for ln in open(mp):
            if ln.startswith("#EXTINF"): a += float(ln.split(":")[1].split(",")[0])
    except Exception: pass
    return a / 60

def remux(tmp, outdir, transcode):
    mp = os.path.join(outdir, "index.m3u8")
    for f in os.listdir(outdir):
        if f.startswith("seg_") and f.endswith(".ts"): rm(os.path.join(outdir, f))
    rm(mp)
    vopts = (["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
              "-vf", "scale='min(1920,iw)':-2", "-threads", "6"] if transcode
             else ["-c:v", "copy", "-bsf:v", "h264_mp4toannexb"])
    cmd = (["nice", "-n", "15", "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
            "-err_detect", "ignore_err", "-fflags", "+genpts", "-i", tmp,
            "-map", "0:v:0", "-map", "0:a:0"] + vopts +
           ["-c:a", "aac", "-ac", "2", "-b:a", "192k", "-ar", "48000",
            "-f", "hls", "-hls_time", "10", "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_filename", os.path.join(outdir, "seg_%04d.ts"), mp])
    try:
        subprocess.run(cmd, capture_output=True, timeout=(14400 if transcode else 3600), text=True)
    except subprocess.TimeoutExpired:
        return -1
    return hls_minutes(mp) if os.path.exists(mp) else -1

ok = fail = 0
for sid in ids:
    m = mv.get(sid); c = cat.get(sid)
    if not m or not c:
        log("skip %s (no metadata/catalog)" % sid); fail += 1; continue
    slug = m["slug"]; outdir = os.path.join(DISK, slug); os.makedirs(outdir, exist_ok=True)
    ext = (c.get("container_extension") or "mkv").lower()
    url = "http://%s/movie/%s/%s/%s.%s" % (vi.HOST, vi.USER, vi.PW, sid, ext)
    tmp = os.path.join(DISK, ".repull_%d.%s" % (sid, ext))
    log("REPULL %s '%s' — downloading" % (sid, slug[:40]))
    try:
        subprocess.run(["curl", "-sL", "-A", UA, "--retry", "5", "--retry-delay", "5",
                        "--retry-all-errors", "-o", tmp, url], timeout=3000)
    except subprocess.TimeoutExpired:
        log("  download TIMEOUT"); rm(tmp); fail += 1; continue
    if not os.path.exists(tmp) or os.path.getsize(tmp) < 5_000_000:
        log("  download too small/failed"); rm(tmp); fail += 1; continue
    vcodec = ffprobe1(tmp, "stream=codec_name", stream=True).splitlines()[0] if ffprobe1(tmp, "stream=codec_name", stream=True) else ""
    try: fdur = float(ffprobe1(tmp, "format=duration") or 0)
    except Exception: fdur = 0
    log("  downloaded %dMB, local=%s %.0fmin" % (os.path.getsize(tmp) // 1048576, vcodec, fdur / 60))
    got = remux(tmp, outdir, transcode=(vcodec != "h264"))
    if 0 < fdur and got < fdur / 60 * 0.9 and vcodec == "h264":
        log("  copy came up short (%.0f/%.0fmin) — transcoding past the glitch" % (got, fdur / 60))
        got = remux(tmp, outdir, transcode=True)
    rm(tmp)
    if got < 5:
        log("  FAIL remux (%s)" % slug); fail += 1; continue
    try:
        if c.get("stream_icon"):
            r2 = urllib.request.Request(c["stream_icon"], headers={"User-Agent": UA})
            open(os.path.join(outdir, "poster.jpg"), "wb").write(urllib.request.urlopen(r2, timeout=30).read())
    except Exception: pass
    log("  OK %s -> %.0fmin (meta %.0fmin)%s" % (slug, got, fdur / 60,
        "" if (fdur and got >= fdur / 60 * 0.9) else "  STILL SHORT"))
    ok += 1
log("REPULL BATCH DONE: ok=%d fail=%d" % (ok, fail))
