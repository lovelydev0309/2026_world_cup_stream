#!/usr/bin/env python3
"""
Concurrent (N-way) faststart re-upload of the MAJO Telegram-storage films.

Speeds up the single-stream uploader by running several films at once, each on its
own bot MTProto connection (upload is network-bound, so this stacks throughput without
loading CPU). Muxes are nice/ionice'd so they never starve the live channels.

- Skips films already fixed this run (catalog entry with "fixed": true or a fresh msg id).
- Each worker: HLS -> faststart MP4 (+thumbnail +video attrs) -> upload -> catalog.
- Main thread is the ONLY catalog writer (workers just return results) -> no file race.
- Deletes the old broken originals at the end.

Usage:  cd /opt/streaming-stack && SHOP=majo nohup python3 telegram/tg_reupload_fast.py > logs/tg_fast.log 2>&1 &
"""
import os, subprocess, json, threading, asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
from telethon.sync import TelegramClient
from telethon.tl.types import DocumentAttributeVideo

BASEDIR = os.path.dirname(os.path.abspath(__file__))
def cfg(k, d=None):
    p = os.path.join(BASEDIR, "..", "config", "accounts.env")
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                kk, vv = line.split("=", 1)
                if kk.strip() == k: return vv.strip()
    except FileNotFoundError:
        pass
    return d

SHOP    = os.environ.get("SHOP", "majo").lower()
API_ID  = int(cfg("TG_API_ID")); API_HASH = cfg("TG_API_HASH")
TOK     = cfg("TG_BOT_TOKEN") if SHOP == "kozee" else cfg("%s_BOT_TOKEN" % SHOP.upper())
CHAN    = int(cfg("TG_STORAGE_CHANNEL"))
DISK    = "/opt/streaming-stack/vod-disk-us" if SHOP == "kozee" else "/opt/streaming-stack/vod-disk"
CATALOG = os.path.join(BASEDIR, "..", "config", "tg_movies_%s.json" % SHOP)
WEB_CATALOG = ("/opt/streaming-stack/player/tg-mx/movies-tg.json" if SHOP == "majo"
               else "/opt/streaming-stack/player/tg/movies-tg.json")
WORKERS = int(os.environ.get("WORKERS", "3"))
NICE    = ["nice", "-n", "19", "ionice", "-c3"]     # muxes yield to live channels
OLD_MSG_RANGE = range(6, 21)                          # original broken uploads to purge at the end

def log(m): print(m, flush=True)

# ---- per-film media prep (same recipe as the fixed batch) ------------------
def mux(slug):
    mp4 = "/tmp/%s.mp4" % slug
    try:
        subprocess.run(NICE + ["ffmpeg", "-y", "-loglevel", "error",
                        "-i", DISK + "/" + slug + "/index.m3u8",
                        "-c", "copy", "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart", mp4], check=True, timeout=2400)
    except Exception as e:
        log("  mux-fail %s: %s" % (slug, e)); return None
    if not os.path.exists(mp4) or os.path.getsize(mp4) < 5e6:
        log("  bad mp4 %s" % slug); return None
    return mp4

def probe(mp4):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height:format=duration",
                              "-of", "json", mp4], capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out); st = j["streams"][0]
        return int(st["width"]), int(st["height"]), int(float(j["format"]["duration"]))
    except Exception:
        return 0, 0, 0

def thumbnail(mp4, slug):
    th = "/tmp/%s_th.jpg" % slug
    try:
        subprocess.run(NICE + ["ffmpeg", "-y", "-loglevel", "error", "-ss", "120", "-i", mp4,
                        "-frames:v", "1", "-vf", "scale=320:-1", th], check=True, timeout=120)
        if os.path.exists(th) and os.path.getsize(th) > 1000:
            return th
    except Exception:
        pass
    return None

# ---- per-thread Telethon client (clients are not shareable across threads) --
_tl = threading.local()
def client_for_thread():
    c = getattr(_tl, "client", None)
    if c is None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        name = threading.current_thread().name.replace("/", "_")
        c = TelegramClient(os.path.join(BASEDIR, "..", "config", "majo_up_%s" % name), API_ID, API_HASH)
        c.start(bot_token=TOK)
        _tl.client = c
        _tl.ent = c.get_entity(CHAN)
    return c, _tl.ent

def worker(m):
    slug = m["slug"]
    mp4 = mux(slug)
    if not mp4:
        return (m, None)
    w, h, dur = probe(mp4)
    attrs = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)] if w and h else None
    th = thumbnail(mp4, slug)
    cap = "%s%s" % (m.get("title") or slug, "  (%s)" % m.get("year") if m.get("year") else "")
    try:
        client, ent = client_for_thread()
        msg = client.send_file(ent, mp4, caption=cap, supports_streaming=True, attributes=attrs, thumb=th)
        mid = msg.id
    except Exception as e:
        log("  upload-fail %s: %s" % (slug, str(e)[:120])); mid = None
    for f in (mp4, th):
        try:
            if f: os.remove(f)
        except Exception: pass
    return (m, mid)

# ---- work list -------------------------------------------------------------
done = {}
if os.path.exists(CATALOG):
    for e in json.load(open(CATALOG)):
        done[e["slug"]] = e                      # films already re-done this run (keep them)

meta = {mm["slug"]: mm for mm in json.load(open(DISK + "/movies.json"))}
def dsize(slug):
    p = DISK + "/" + slug
    try: return sum(os.path.getsize(p + "/" + f) for f in os.listdir(p) if f.endswith(".ts"))
    except Exception: return 1e12

SLUGS = [s.strip() for s in os.environ.get("SLUGS", "").split(",") if s.strip()]
if SLUGS:
    # explicit curated selection -> fresh catalog rebuild (ignore whatever's there)
    done = {}
    remaining = [meta[s] for s in SLUGS if s in meta]
    missing = [s for s in SLUGS if s not in meta]
    if missing: log("  WARN slugs not in catalog: %s" % missing)
    log("fast re-upload (curated): %d films, %d workers" % (len(remaining), WORKERS))
else:
    pool = [mm for mm in meta.values() if not mm.get("hidden") and mm.get("slug")
            and isinstance(mm.get("rating"), (int, float)) and dsize(mm["slug"]) < 1.9e9]
    pool.sort(key=lambda mm: -mm["rating"])
    intended = pool[:15]
    remaining = [mm for mm in intended if mm["slug"] not in done]
    log("fast re-upload: %d already fixed, %d remaining, %d workers" % (len(done), len(remaining), WORKERS))

def write_catalog():
    out = list(done.values())
    tmp = CATALOG + ".tmp"; json.dump(out, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, CATALOG)
    try: json.dump(out, open(WEB_CATALOG, "w"), ensure_ascii=False, indent=1)
    except Exception: pass

# ---- run concurrently, main thread writes the catalog ----------------------
ok = 0
with ThreadPoolExecutor(max_workers=WORKERS) as ex:
    futs = {ex.submit(worker, m): m for m in remaining}
    for fut in as_completed(futs):
        m, mid = fut.result()
        if not mid:
            continue
        done[m["slug"]] = {"slug": m["slug"], "title": m.get("title"), "year": m.get("year"),
                           "rating": m.get("rating"), "genre": m.get("genre"),
                           "duration": m.get("duration"), "message_id": mid, "fixed": True}
        write_catalog(); ok += 1
        log("  OK %s -> msg %s" % (m["slug"], mid))

# ---- purge the old broken originals ---------------------------------------
try:
    asyncio.set_event_loop(asyncio.new_event_loop())
    cc = TelegramClient(os.path.join(BASEDIR, "..", "config", "majo_up_cleanup"), API_ID, API_HASH)
    cc.start(bot_token=TOK); ent = cc.get_entity(CHAN)
    keep = {e["message_id"] for e in done.values()}
    dm = os.environ.get("DELETE_MSGS", "").strip()
    if dm:
        want = []
        for part in dm.split(","):
            part = part.strip()
            if "-" in part:
                a, b = part.split("-"); want += list(range(int(a), int(b) + 1))
            elif part.isdigit():
                want.append(int(part))
        victims = [i for i in want if i not in keep]
    else:
        victims = [i for i in OLD_MSG_RANGE if i not in keep]
    if victims:
        cc.delete_messages(ent, victims)
        log("  purged old broken msgs: %s" % victims)
    cc.disconnect()
except Exception as e:
    log("  cleanup skipped: %s" % str(e)[:100])

log("FAST DONE: +%d films, catalog now %d on Telegram" % (ok, len(done)))
