#!/usr/bin/env python3
"""
Pick the genuinely-good MAJO films to put on Telegram, from the OMDb-rerated catalog.
Filters to real IMDb ratings, drops obscure low-vote entries (fetches imdbVotes via the
stored imdb_id), drops adult/oversized/truncated, and prints a ranked shortlist.
Read-only: writes nothing except stdout. OMDb key from accounts.env.
"""
import os, json, time, urllib.request, urllib.parse

def cfg(k, d=None):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "accounts.env")
    for line in open(p):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            kk, vv = line.split("=", 1)
            if kk.strip() == k: return vv.strip()
    return d

DISK = "/opt/streaming-stack/vod-disk"
KEY  = cfg("OMDB_API_KEY")
API  = "https://www.omdbapi.com/"
MINR = float(os.environ.get("MINR", "7.0"))
MINV = int(os.environ.get("MINV", "2000"))     # min IMDb votes -> filters obscure/low-vote entries
ADULT = ("striptease", "aerobic", "xxx", "porn", "erotic")

def dsize(slug):
    p = DISK + "/" + slug
    try: return sum(os.path.getsize(p + "/" + f) for f in os.listdir(p) if f.endswith(".ts"))
    except Exception: return 1e12

def votes(imdb_id):
    if not imdb_id: return 0
    q = urllib.parse.urlencode({"i": imdb_id, "apikey": KEY})
    for _ in range(3):
        try:
            with urllib.request.urlopen(API + "?" + q, timeout=20) as r:
                d = json.load(r)
            v = (d.get("imdbVotes") or "0").replace(",", "")
            return int(v) if v.isdigit() else 0
        except Exception:
            time.sleep(0.4)
    return 0

d = json.load(open(DISK + "/movies.json"))
pool = [m for m in d if m.get("rating_source") == "imdb" and isinstance(m.get("rating"), (int, float))
        and m["rating"] >= MINR and not m.get("hidden") and not m.get("truncated")
        and not any(a in ((m.get("title") or "") + " " + (m.get("genre") or "")).lower() for a in ADULT)]
pool = [m for m in pool if dsize(m["slug"]) < 1.9e9]
pool.sort(key=lambda m: -m["rating"])
print("size+rating pool (>=%.1f, complete, <1.9GB, non-adult): %d films; fetching votes..." % (MINR, len(pool)))

sel = []
for m in pool:
    v = votes(m.get("imdb_id"))
    m["_votes"] = v
    if v >= MINV:
        sel.append(m)
    time.sleep(0.03)

sel.sort(key=lambda m: -m["rating"])
print("=== GOOD SHORTLIST (votes>=%d): %d films ===" % (MINV, len(sel)))
for i, m in enumerate(sel[:30], 1):
    print("  %2d. %.1f  %6dk votes  %4dMB  %s (%s)"
          % (i, m["rating"], m["_votes"] // 1000, int(dsize(m["slug"]) // 1048576),
             m.get("title"), m.get("year")))
# emit the slugs for the top 15 as a machine-readable line
print("TOP15_SLUGS=" + ",".join(m["slug"] for m in sel[:15]))
