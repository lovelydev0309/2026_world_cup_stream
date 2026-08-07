#!/usr/bin/env python3
"""
Prune the MAJO VOD disk down to the KEEP set (curated, shown films) and delete the rest
to free space for higher-rated titles. Destructive but recoverable: writes a manifest of
every deleted film (slug, stream_id, title, year, rating, genre) so any can be re-pulled
from the provider later. Also removes the deleted entries from movies.json (backed up).

KEEP  = shown films: has m3u8_url, not hidden, not truncated.
DELETE= every other entry in movies.json + any on-disk film dir not in movies.json (orphans).

Usage:
  dry run (default): majo_prune.py                 -> measures + writes manifest, deletes NOTHING
  apply:             majo_prune.py --apply         -> deletes dirs + rewrites movies.json
"""
import json, os, sys, shutil, time

DISK = "/opt/streaming-stack/vod-disk"
SRC = DISK + "/movies.json"
APPLY = "--apply" in sys.argv
MANIFEST_DIR = "/opt/streaming-stack/config"

def dir_bytes(slug):
    p = os.path.join(DISK, slug or "")
    try: return sum(os.path.getsize(os.path.join(p, f)) for f in os.listdir(p))
    except Exception: return 0

d = json.load(open(SRC))
keep, delete = [], []
for m in d:
    shown = bool(m.get("m3u8_url")) and not m.get("hidden") and not m.get("truncated")
    (keep if shown else delete).append(m)

keep_slugs = {m.get("slug") for m in keep}
del_slugs = {m.get("slug") for m in delete}

# on-disk dirs not present in movies.json at all = orphans (also unnecessary)
ondisk = set()
for f in os.listdir(DISK):
    if os.path.isdir(os.path.join(DISK, f)):
        ondisk.add(f)
orphans = sorted(ondisk - keep_slugs - del_slugs)

del_bytes = sum(dir_bytes(m.get("slug")) for m in delete)
orph_bytes = sum(dir_bytes(s) for s in orphans)

print("KEEP  : %d films (curated/shown)" % len(keep))
print("DELETE: %d catalog films (hidden/unrated/low/truncated) = %.1f GB"
      % (len(delete), del_bytes / 1e9))
print("ORPHAN: %d on-disk dirs not in catalog = %.1f GB" % (len(orphans), orph_bytes / 1e9))
print("TOTAL to free: %.1f GB" % ((del_bytes + orph_bytes) / 1e9))

# recovery manifest (so any deleted film can be re-pulled by stream_id)
manifest = {
    "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(SRC))),
    "kept": len(keep),
    "deleted": [{"slug": m.get("slug"), "stream_id": m.get("stream_id"),
                 "title": m.get("title"), "year": m.get("year"),
                 "rating": m.get("rating"), "genre": m.get("genre")} for m in delete],
    "orphans": orphans,
}
mpath = os.path.join(MANIFEST_DIR, "majo_pruned_manifest.json")
json.dump(manifest, open(mpath, "w"), ensure_ascii=False, indent=1)
print("manifest written:", mpath, "(%d deleted films recorded)" % len(delete))

if not APPLY:
    print("\nDRY RUN — nothing deleted. Re-run with --apply to delete + rewrite movies.json.")
    print("sample to delete:", [m.get("title") or m.get("slug") for m in delete[:8]])
    sys.exit(0)

# ---- apply: back up movies.json, delete dirs, keep only the 143 ----
bak = SRC + ".bak.prune-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime())
open(bak, "w").write(json.dumps(d, ensure_ascii=False))
print("backup:", bak)

removed = freed = 0
for m in delete:
    p = os.path.join(DISK, m.get("slug") or "")
    if m.get("slug") and os.path.isdir(p):
        freed += dir_bytes(m.get("slug"))
        shutil.rmtree(p, ignore_errors=True); removed += 1
for s in orphans:
    p = os.path.join(DISK, s)
    if os.path.isdir(p):
        freed += dir_bytes(s)
        shutil.rmtree(p, ignore_errors=True); removed += 1

tmp = SRC + ".tmp"; json.dump(keep, open(tmp, "w"), ensure_ascii=False); os.replace(tmp, SRC)
print("APPLIED: removed %d dirs, freed %.1f GB, movies.json now %d films"
      % (removed, freed / 1e9, len(keep)))
