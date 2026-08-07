#!/usr/bin/env python3
"""
Batch-upload complete movies to the Telegram storage channel via Telethon (bot over
MTProto -> up to 2 GB, Telegram's storage, no CDN). Records a catalog of what's
actually on Telegram: config/tg_movies_<shop>.json = [{slug,title,year,rating,
genre,duration,message_id}]. Idempotent (skips already-uploaded). The bot shows/serves
ONLY these. Secrets from accounts.env.

CRITICAL for playback: the MP4 must be faststart (moov atom at the FRONT) and carry
explicit DocumentAttributeVideo (w/h/duration/supports_streaming) + a thumbnail, or
Telegram shows a blank thumbnail and a black player (index is at the tail, unstreamable).

Usage:
  new films:  SHOP=majo nohup python3 telegram/tg_upload_batch.py 15 > logs/tg_upload.log 2>&1 &
  re-do all:  SHOP=majo REUPLOAD=1 nohup python3 telegram/tg_upload_batch.py > logs/tg_reup.log 2>&1 &
"""
import os, subprocess, json, sys
from telethon.sync import TelegramClient
from telethon.tl.types import DocumentAttributeVideo

def cfg(k, d=None):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "accounts.env")
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                kk, vv = line.split("=", 1)
                if kk.strip() == k: return vv.strip()
    except FileNotFoundError:
        pass
    return d

SHOP = os.environ.get("SHOP", "majo").lower()
REUP = os.environ.get("REUPLOAD", "").lower() in ("1", "true", "yes")
API_ID = int(cfg("TG_API_ID")); API_HASH = cfg("TG_API_HASH")
TOK = cfg("TG_BOT_TOKEN") if SHOP == "kozee" else cfg("%s_BOT_TOKEN" % SHOP.upper())
CHAN = int(cfg("TG_STORAGE_CHANNEL"))
DISK = "/opt/streaming-stack/vod-disk-us" if SHOP == "kozee" else "/opt/streaming-stack/vod-disk"
BASEDIR = os.path.dirname(os.path.abspath(__file__))
CATALOG = os.path.join(BASEDIR, "..", "config", "tg_movies_%s.json" % SHOP)
WEB_CATALOG = ("/opt/streaming-stack/player/tg-mx/movies-tg.json" if SHOP == "majo"
               else "/opt/streaming-stack/player/tg/movies-tg.json")   # served for the poster Mini App
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 15

def log(m): print(m, flush=True)

def mux(slug):
    """HLS -> faststart MP4. Returns mp4 path or None."""
    mp4 = "/tmp/%s.mp4" % slug
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", DISK + "/" + slug + "/index.m3u8",
                        "-c", "copy", "-bsf:a", "aac_adtstoasc",
                        "-movflags", "+faststart", mp4], check=True, timeout=2400)
    except Exception as e:
        log("  mux-fail %s: %s" % (slug, e)); return None
    if not os.path.exists(mp4) or os.path.getsize(mp4) < 5e6:
        log("  bad mp4 %s" % slug); return None
    return mp4

def probe(mp4):
    """(w, h, duration_sec) from the muxed file, so Telegram gets real video metadata."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                              "-show_entries", "stream=width,height:format=duration",
                              "-of", "json", mp4], capture_output=True, text=True, timeout=60).stdout
        j = json.loads(out); st = j["streams"][0]
        return int(st["width"]), int(st["height"]), int(float(j["format"]["duration"]))
    except Exception:
        return 0, 0, 0

def thumbnail(mp4, slug):
    """A single JPEG frame -> Telegram shows a real poster instead of a black tile."""
    th = "/tmp/%s_th.jpg" % slug
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "120", "-i", mp4,
                        "-frames:v", "1", "-vf", "scale=320:-1", th],
                       check=True, timeout=120)
        if os.path.exists(th) and os.path.getsize(th) > 1000:
            return th
    except Exception:
        pass
    return None

def upload(client, ent, m, mp4):
    """Upload with faststart mp4 + real video attributes + thumbnail. Returns msg or None."""
    cap = "%s%s" % (m.get("title") or m["slug"], "  (%s)" % m.get("year") if m.get("year") else "")
    w, h, dur = probe(mp4)
    attrs = [DocumentAttributeVideo(duration=dur, w=w, h=h, supports_streaming=True)] if w and h else None
    th = thumbnail(mp4, m["slug"])
    try:
        msg = client.send_file(ent, mp4, caption=cap, supports_streaming=True,
                               attributes=attrs, thumb=th)
    except Exception as e:
        log("  upload-fail %s: %s" % (m["slug"], str(e)[:120])); msg = None
    for f in (mp4, th):
        try:
            if f: os.remove(f)
        except Exception: pass
    return msg

# ---- build work list -------------------------------------------------------
existing = {}
if os.path.exists(CATALOG):
    for e in json.load(open(CATALOG)):
        existing[e["slug"]] = e

if REUP:
    # Re-do every film already in the catalog (fix the black-player uploads); replace
    # message_ids in place and delete the old broken messages afterward.
    todo = list(existing.values())
    log("REUPLOAD: re-doing %d catalog films with faststart+attrs (shop=%s)" % (len(todo), SHOP))
else:
    d = json.load(open(DISK + "/movies.json"))
    pool = [m for m in d if not m.get("hidden") and m.get("slug") and m["slug"] not in existing
            and isinstance(m.get("rating"), (int, float))]
    def dsize(slug):
        p = DISK + "/" + slug
        try: return sum(os.path.getsize(p + "/" + f) for f in os.listdir(p) if f.endswith(".ts"))
        except Exception: return 1e12
    pool = [m for m in pool if dsize(m["slug"]) < 1.9e9]     # keep under the 2GB bot limit
    pool.sort(key=lambda m: -m["rating"])
    todo = pool[:LIMIT]
    log("to upload: %d movies (shop=%s, already have %d)" % (len(todo), SHOP, len(existing)))

# ---- run -------------------------------------------------------------------
client = TelegramClient(os.path.join(BASEDIR, "..", "config", "%s_uploader_tl" % SHOP), API_ID, API_HASH)
client.start(bot_token=TOK)
ent = client.get_entity(CHAN)

def write_catalog(out):
    tmp = CATALOG + ".tmp"; json.dump(out, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, CATALOG)
    try: json.dump(out, open(WEB_CATALOG, "w"), ensure_ascii=False, indent=1)   # for the poster Mini App
    except Exception: pass

out = [] if REUP else list(existing.values())
ok = 0
for m in todo:
    slug = m["slug"]
    mp4 = mux(slug)
    if not mp4:
        if REUP: out.append(m)   # keep the old entry if we couldn't re-mux
        continue
    old_mid = m.get("message_id") if REUP else None
    msg = upload(client, ent, m, mp4)
    if not msg:
        if REUP: out.append(m)
        continue
    entry = {"slug": slug, "title": m.get("title"), "year": m.get("year"), "rating": m.get("rating"),
             "genre": m.get("genre"), "duration": m.get("duration"), "message_id": msg.id}
    out.append(entry); ok += 1
    write_catalog(out)
    if REUP and old_mid:
        try: client.delete_messages(ent, [int(old_mid)])   # remove the black-player original
        except Exception as e: log("  (could not delete old msg %s: %s)" % (old_mid, str(e)[:60]))
    sz = getattr(getattr(msg, "file", None), "size", 0) or 0
    log("  OK %s -> msg %s (%d MB)%s" % (slug, msg.id, sz // 1048576,
                                         "  [replaced %s]" % old_mid if old_mid else ""))
client.disconnect()
log("%s DONE: %d films, catalog now %d on Telegram" % ("REUPLOAD" if REUP else "BATCH", ok, len(out)))
