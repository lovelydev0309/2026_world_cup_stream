#!/usr/bin/env python3
"""
Post-ingest curation for a VOD movies.json (reversible — only sets flags, deletes nothing):
  - tag hand-picked stream_ids as new_release=true (the "Now Showing" row); never hidden
  - quality filter: hidden=true for movies whose REAL rating is below <threshold> or is
    missing (no verifiable IMDb rating), EXCEPT new_release titles which always show.

Run AFTER omdb_rerate.py (so `rating` holds the real IMDb value). Backs up first.
Usage: vod_curate.py <movies.json> <threshold> [new_release_ids_file]
"""
import json, os, sys, time

SRC = sys.argv[1]
THRESH = float(sys.argv[2]) if len(sys.argv) > 2 else 7.5
NRF = sys.argv[3] if len(sys.argv) > 3 else None

newids = set()
if NRF and os.path.exists(NRF):
    for l in open(NRF):
        l = l.strip()
        if l.isdigit():
            newids.add(int(l))

d = json.load(open(SRC))
open(SRC + ".bak.curate-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime()), "w").write(
    json.dumps(d, ensure_ascii=False))

shown = hidden = nr = 0
for m in d:
    try: sid = int(m.get("stream_id", 0))
    except Exception: sid = 0
    isnew = sid in newids
    m["new_release"] = bool(isnew)
    if isnew: nr += 1
    r = m.get("rating")
    try: rv = float(r) if r is not None else None
    except Exception: rv = None
    if isnew:
        m["hidden"] = False; shown += 1
    elif rv is None or rv < THRESH:
        m["hidden"] = True; hidden += 1
    else:
        m["hidden"] = False; shown += 1

tmp = SRC + ".tmp"; open(tmp, "w").write(json.dumps(d, ensure_ascii=False)); os.replace(tmp, SRC)
print("CURATE: total=%d shown=%d hidden=%d new_release=%d threshold=%.1f"
      % (len(d), shown, hidden, nr, THRESH))
