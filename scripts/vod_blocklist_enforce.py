#!/usr/bin/env python3
"""Enforce the permanent VOD blocklist.

Any slug listed in config/vod_blocklist.txt is stripped from every served movie JSON
(vod-disk/movies.json = what the CDN serves, player/vod/movies.json, and
vod-disk/movies_all.json = the catalog source), and its on-disk folder is removed so it
can never be re-served as playable. Runs every minute from cron as a catch-all, so a
blocklisted title (e.g. undertone) can NEVER reappear no matter which ingest/publish
script re-adds it. Writes are atomic and only happen when something actually changed.
"""
import json, os, shutil

DISK = "/opt/streaming-stack/vod-disk"
PLAYER = "/opt/streaming-stack/player/vod"
BLOCKLIST = "/opt/streaming-stack/config/vod_blocklist.txt"
JSON_FILES = [os.path.join(DISK, "movies.json"),
              os.path.join(PLAYER, "movies.json"),
              os.path.join(DISK, "movies_all.json")]

def load_blocklist():
    try:
        return {l.strip().lower() for l in open(BLOCKLIST)
                if l.strip() and not l.startswith("#")}
    except Exception:
        return set()

def strip(path, blocked):
    try:
        data = json.load(open(path))
    except Exception:
        return 0
    if not isinstance(data, list):
        return 0
    kept = [m for m in data if str(m.get("slug", "")).lower() not in blocked]
    removed = len(data) - len(kept)
    if removed:
        tmp = path + ".tmp"
        json.dump(kept, open(tmp, "w"), ensure_ascii=False, indent=1)
        os.replace(tmp, path)   # atomic — a fetch never sees a half-written file
    return removed

def main():
    blocked = load_blocklist()
    if not blocked:
        return
    for p in JSON_FILES:
        if os.path.exists(p):
            strip(p, blocked)
    for slug in blocked:
        d = os.path.join(DISK, slug)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)   # never playable again

if __name__ == "__main__":
    main()
