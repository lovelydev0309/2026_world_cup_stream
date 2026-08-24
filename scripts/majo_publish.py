#!/usr/bin/env python3
"""
Publish the MAJO VOD catalog into two files, deterministically:
  movies_all.json  = full catalog (every known film) with recomputed truncated/hidden flags
  movies.json      = the SERVED catalog: only films that are (a) NOT hidden (quality bar)
                     AND (b) actually playable on disk (index.m3u8 + segments + poster).

This is bulletproof against a stale/incorrect `hidden` flag: a film with no video is
dropped no matter what its flag says, and a low-rated/unrated film is dropped even if
its files exist. Consumers that don't filter hidden (the majotv.com WordPress plugin)
therefore never see broken or low-quality entries. Missing posters for otherwise-good
films are regenerated from a frame. THRESH via env (default 5.5).
"""
import json, os, subprocess, sys

DISK = os.environ.get("VOD_DISK", "/opt/streaming-stack/vod-disk")
THRESH = float(os.environ.get("THRESH", "5.5"))

def hms(t):
    try:
        p = [int(x) for x in (t or "0").split(":")]
        while len(p) < 3: p = [0] + p
        return p[0]*3600 + p[1]*60 + p[2]
    except Exception:
        return 0

def actual_dur(slug):
    a = 0.0
    try:
        for ln in open(os.path.join(DISK, slug, "index.m3u8")):
            if ln.startswith("#EXTINF"):
                try: a += float(ln.split(":")[1].split(",")[0])
                except Exception: pass
    except Exception:
        return 0.0
    return a

def has_video(slug):
    p = os.path.join(DISK, slug or "")
    try:
        return os.path.isdir(p) and os.path.exists(os.path.join(p, "index.m3u8")) \
               and any(f.endswith(".ts") for f in os.listdir(p))
    except Exception:
        return False

def has_poster(slug):
    return os.path.exists(os.path.join(DISK, slug or "", "poster.jpg"))

# 1) union of every known film entry (prefer the richer one, i.e. with a real rating)
seen = {}
for fn in ("movies_all.json", "movies.json"):
    p = os.path.join(DISK, fn)
    if not os.path.exists(p): continue
    try: rows = json.load(open(p))
    except Exception: rows = []
    for m in rows:
        s = m.get("slug")
        if not s: continue
        if s not in seen or (m.get("rating_source") == "imdb" and seen[s].get("rating_source") != "imdb"):
            seen[s] = m
full = list(seen.values())

# 1b) permanent blocklist — slugs in config/vod_blocklist.txt NEVER enter the catalog,
# no matter what a source file contains (e.g. undertone re-added by a re-ingest).
try:
    _blpath = os.path.join(os.path.dirname(DISK.rstrip("/")), "config", "vod_blocklist.txt")
    _blocked = {l.strip().lower() for l in open(_blpath) if l.strip() and not l.startswith("#")}
except Exception:
    _blocked = set()
if _blocked:
    full = [m for m in full if str(m.get("slug", "")).lower() not in _blocked]

# 2) recompute truncated (missing/short HLS) + hidden (broken OR rating<THRESH OR unrated)
for m in full:
    s = m.get("slug", "")
    meta = hms(m.get("duration")); a = actual_dur(s)
    trunc = (not has_video(s)) or (meta >= 300 and a < meta * 0.85)
    m["truncated"] = bool(trunc)
    r = m.get("rating")
    try: rv = float(r) if r is not None else None
    except Exception: rv = None
    m["hidden"] = bool(trunc or ((not m.get("featured")) and (rv is None or rv < THRESH)))

# 3) served = not hidden AND playable (video+poster); regenerate a missing poster if video exists
served = []
for m in full:
    s = m.get("slug", "")
    if m["hidden"] or not has_video(s):
        continue
    if not has_poster(s):
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "180",
                            "-i", os.path.join(DISK, s, "index.m3u8"), "-frames:v", "1",
                            "-vf", "scale=400:-1", os.path.join(DISK, s, "poster.jpg")], timeout=90)
        except Exception:
            pass
        if not has_poster(s):
            continue
    served.append(m)

# 4) order the served catalog by real quality: recognizable/high-rated first. The metadata has
# no vote counts, so a plain rating sort leads with a few obscure no-vote shorts inflated to
# 10.0 (e.g. sports mini-movies) ABOVE genuine classics; demote anything rated >9.3 (no real
# film beats Shawshank's 9.3 on IMDb) below the legit top band. Ties broken by newer year.
def _quality(m):
    r = m.get("rating"); rv = r if isinstance(r, (int, float)) else 0
    try: y = int(str(m.get("year") or "0")[:4])
    except Exception: y = 0
    if m.get("rating_source") != "imdb":
        eff = min(rv, 7.4)                     # provider-sourced rating: unreliable -> cap out of the top band
    elif y >= 2022 and rv >= 8.7:
        eff = min(rv, 7.8)                     # very-recent + very-high IMDb = low-vote anomaly (e.g. obscure fest films)
    elif rv > 9.3:
        eff = rv - 3.0                          # no genuine film beats Shawshank's 9.3 -> demote inflated
    else:
        eff = rv
    return (0 if m.get("featured") else 1, -eff, -y)
served.sort(key=_quality)

json.dump(full, open(DISK + "/movies_all.json", "w"), ensure_ascii=False, indent=1)
tmp = DISK + "/movies.json.tmp"
json.dump(served, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, DISK + "/movies.json")
print("PUBLISH: full=%d  served(playable, rating>=%.1f)=%d  hidden/broken=%d"
      % (len(full), THRESH, len(served), len(full) - len(served)))
