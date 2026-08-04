#!/usr/bin/env python3
"""
Auto-add-on-digital-release watcher.

Keeps a watchlist of titles that were theatrical-only when requested (e.g. current
blockbusters). Each run it checks the provider VOD catalog; when a watched title
finally appears there (= it got a legitimate digital release the provider carries),
it ingests that exact stream_id into the US catalog, adds it to the New Releases
set, pulls real IMDb ratings, and re-applies the quality filter. No cams, ever —
it only ever adds what the provider legitimately publishes.

Run daily via cron:  nice -n 15 flock -n /tmp/vodwatch.lock python3 scripts/vod_watch.py
Dry run (report matches, ingest nothing):  python3 scripts/vod_watch.py --dry

Files (on the US movie disk):
  _watchlist.json    {"pending":[{title,year,aliases[]}], "added":[...]}
  _blockbusters.ids  the New Releases stream_id set (grows as titles are auto-added)
"""
import json, os, re, subprocess, sys, time, urllib.request

DRY    = "--dry" in sys.argv
ROOT   = "/opt/streaming-stack"
DISK   = os.environ.get("VOD_DISK", ROOT + "/vod-disk-us")
WL     = DISK + "/_watchlist.json"
IDS    = DISK + "/_blockbusters.ids"
MOVIES = DISK + "/movies.json"
UA     = "okhttp/4.9.3"

def acct(k):
    try:
        for line in open(ROOT + "/config/accounts.env"):
            line = line.strip()
            if line.startswith(k + "="): return line.split("=", 1)[1].strip()
    except Exception: pass
    return None

HOST = acct("VOD_HOST") or "tvon247.com"; USER = acct("VOD_USER"); PW = acct("VOD_PW")
API = "http://%s/player_api.php?username=%s&password=%s" % (HOST, USER, PW)

def log(m): print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), m), flush=True)

def norm(s):
    s = re.sub(r'^\s*(EN|US|LAT|ES|BR|IN|LT|FR|IT|DE)\s*-\s*', '', s or '', flags=re.I)  # lang prefix
    s = re.sub(r'\[.*?\]', '', s)                    # [MULTI-SUB] etc.
    s = re.sub(r'\b(19|20)\d\d\b', '', s)            # year
    s = re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()
    return s

def run(cmd, env=None):
    e = dict(os.environ); e.update(env or {})
    return subprocess.run(cmd, cwd=ROOT, env=e, capture_output=True, text=True)

def main():
    if not os.path.exists(WL):
        log("no watchlist at %s" % WL); return
    wl = json.load(open(WL)); pending = wl.get("pending", [])
    if not pending:
        log("watchlist empty; nothing to watch"); return
    # never run two ingests at once (races the provider connection limit + movies.json)
    if not DRY and subprocess.run(["pgrep","-f","vod_ingest2.py"], capture_output=True).returncode == 0:
        log("an ingest is already running; skipping this cycle"); return
    try:
        req = urllib.request.Request(API + "&action=get_vod_streams", headers={"User-Agent": UA})
        cat = json.load(urllib.request.urlopen(req, timeout=60))
    except Exception as e:
        log("catalog fetch failed: %s" % e); return
    log("watching %d title(s) against %d catalog entries%s" % (len(pending), len(cat), "  [DRY]" if DRY else ""))

    found = []
    for w in pending:
        targets = {norm(w["title"])} | {norm(a) for a in w.get("aliases", [])}
        wy = str(w.get("year", ""))
        for c in cat:
            nm = c.get("name", "")
            if not nm.startswith(("EN", "US")): continue
            if norm(nm) in targets and norm(nm):
                cy = re.search(r'\b(20\d\d)\b', nm)
                if wy and cy and int(cy.group(1)) != int(wy): continue   # exact title + exact year
                found.append((w, int(c["stream_id"]), nm)); break
    if not found:
        log("no watched titles available on the provider yet"); return
    for w, sid, nm in found:
        log("AVAILABLE NOW: '%s' -> stream_id %s  (%s)" % (w["title"], sid, nm))
    if DRY:
        log("dry run — not ingesting"); return

    ids = [str(sid) for _, sid, _ in found]
    idf = DISK + "/_watch_ingest.ids"
    open(idf, "w").write("\n".join(ids) + "\n")
    r = run(["python3", "scripts/vod_ingest2.py", str(len(ids) + 2), "30"],
            {"VOD_DISK": DISK, "VOD_US": "1", "VOD_RESTORE": "1", "VOD_RESTORE_FILE": idf})
    log("ingest rc=%s" % r.returncode)

    have = set(x.strip() for x in open(IDS) if x.strip().isdigit()) if os.path.exists(IDS) else set()
    have.update(ids)
    open(IDS, "w").write("\n".join(sorted(have)) + "\n")
    run(["python3", "scripts/omdb_rerate.py", MOVIES])
    run(["python3", "scripts/vod_curate.py", MOVIES, "7.5", IDS])

    done_titles = {w["title"] for w, _, _ in found}
    wl["added"] = wl.get("added", []) + [
        {"title": w["title"], "year": w.get("year"), "stream_id": sid,
         "added_utc": time.strftime("%Y-%m-%d", time.gmtime())} for w, sid, _ in found]
    wl["pending"] = [w for w in pending if w["title"] not in done_titles]
    json.dump(wl, open(WL, "w"), ensure_ascii=False, indent=2)
    log("done: auto-added %d title(s); %d still pending" % (len(found), len(wl["pending"])))

if __name__ == "__main__":
    main()
