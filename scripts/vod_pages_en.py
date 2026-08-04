#!/usr/bin/env python3
"""
Regenerate the per-movie index.html pages of a VOD catalog from movies.json,
using vod_ingest2.write_movie_html (now language-aware). For the US/Kozee catalog
(language == "English") this renders the pages in English with the corrected IMDb
ratings from movies.json. Idempotent — safe to run after each re-rate.

Usage:  VOD_DISK=/opt/streaming-stack/vod-disk-us python3 scripts/vod_pages_en.py
"""
import json, os, subprocess, sys
sys.path.insert(0, "/opt/streaming-stack/scripts")
import vod_ingest2 as vi

DISK = os.environ.get("VOD_DISK", "/opt/streaming-stack/vod-disk-us")
d = json.load(open(DISK + "/movies.json"))
done = skip = 0
for m in d:
    slug = m.get("slug"); outdir = os.path.join(DISK, slug or "")
    mp = os.path.join(outdir, "index.m3u8")
    if not slug or not os.path.exists(mp):
        skip += 1; continue
    w = h = 0
    try:
        pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=width,height", "-of", "csv=p=0", mp],
                            capture_output=True, text=True, timeout=30)
        vals = (pr.stdout or "").strip().replace("\n", ",").split(",")
        w = int(vals[0]); h = int(vals[1])
    except Exception:
        pass
    try:
        vi.write_movie_html(outdir, m, w, h, "h264"); done += 1
    except Exception as ex:
        print("fail %s: %s" % (slug, ex)); skip += 1
print("regenerated %d pages (skipped %d)" % (done, skip))
