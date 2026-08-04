#!/usr/bin/env python3
"""
Post-ingest curation for a VOD movies.json (reversible — only sets flags, deletes nothing):
  - tag hand-picked stream_ids as new_release=true (the "Now Showing" row)
  - mark TRUNCATED movies (actual HLS length < 85% of metadata = incomplete download)
    and always hide them — a broken/half movie must never be shown
  - quality filter: otherwise hidden=true when the real IMDb rating is below <threshold>
    or missing, EXCEPT new_release titles.

Run AFTER omdb_rerate.py. Backs up first.
Usage: vod_curate.py <movies.json> <threshold> [new_release_ids_file]
"""
import json, os, sys, time

SRC = sys.argv[1]
THRESH = float(sys.argv[2]) if len(sys.argv) > 2 else 7.5
NRF = sys.argv[3] if len(sys.argv) > 3 else None
DISK = os.path.dirname(os.path.abspath(SRC))

newids = set()
if NRF and os.path.exists(NRF):
    for l in open(NRF):
        l = l.strip()
        if l.isdigit():
            newids.add(int(l))

def hms(t):
    try:
        p = [int(x) for x in (t or "0").split(":")]
        while len(p) < 3: p = [0] + p
        return p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        return 0

def actual_dur(slug):
    mp = os.path.join(DISK, slug or "", "index.m3u8")
    a = 0.0
    try:
        for ln in open(mp):
            if ln.startswith("#EXTINF"):
                try: a += float(ln.split(":")[1].split(",")[0])
                except Exception: pass
    except Exception:
        return 0.0
    return a

d = json.load(open(SRC))
open(SRC + ".bak.curate-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()), "w").write(
    json.dumps(d, ensure_ascii=False))

shown = hidden = nr = trunc = 0
for m in d:
    try: sid = int(m.get("stream_id", 0))
    except Exception: sid = 0
    isnew = sid in newids
    m["new_release"] = bool(isnew)
    if isnew: nr += 1

    meta = hms(m.get("duration")); a = actual_dur(m.get("slug"))
    truncated = (meta >= 300 and a > 0 and a < meta * 0.85)
    m["truncated"] = bool(truncated)
    if truncated: trunc += 1

    r = m.get("rating")
    try: rv = float(r) if r is not None else None
    except Exception: rv = None

    if truncated:
        m["hidden"] = True; hidden += 1              # never show an incomplete movie
    elif isnew:
        m["hidden"] = False; shown += 1
    elif rv is None or rv < THRESH:
        m["hidden"] = True; hidden += 1
    else:
        m["hidden"] = False; shown += 1

tmp = SRC + ".tmp"; open(tmp, "w").write(json.dumps(d, ensure_ascii=False)); os.replace(tmp, SRC)
print("CURATE: total=%d shown=%d hidden=%d (truncated=%d) new_release=%d threshold=%.1f"
      % (len(d), shown, hidden, trunc, nr, THRESH))
