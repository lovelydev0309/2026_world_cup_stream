#!/usr/bin/env python3
"""POC: upload one complete movie to the Telegram storage channel via Telethon.
A bot over MTProto bypasses the 50 MB HTTP Bot API limit (up to 2 GB), so movies
live on Telegram's storage instead of our CDN. Secrets read from accounts.env.
"""
import os, subprocess, json
from telethon.sync import TelegramClient
from telethon.tl.types import PeerChannel

def cfg(k, d=None):
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "accounts.env")
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                kk, vv = line.split("=", 1)
                if kk.strip() == k:
                    return vv.strip()
    except FileNotFoundError:
        pass
    return d

API_ID = int(cfg("TG_API_ID")); API_HASH = cfg("TG_API_HASH")
TOK = cfg("MAJO_BOT_TOKEN"); CHAN = int(cfg("TG_STORAGE_CHANNEL"))
DISK = "/opt/streaming-stack/vod-disk"

d = json.load(open(DISK + "/movies.json"))
cand = []
for m in d:
    if m.get("hidden") or not m.get("slug"):
        continue
    p = DISK + "/" + m["slug"]
    try:
        sz = sum(os.path.getsize(p + "/" + f) for f in os.listdir(p) if f.endswith(".ts"))
    except Exception:
        continue
    if 60e6 < sz < 300e6:
        cand.append((sz, m))
cand.sort()
m = cand[0][1]; slug = m["slug"]; mp4 = "/tmp/%s.mp4" % slug
print("  test movie:", m.get("title"), "(%dMB HLS)" % (cand[0][0] // 1048576))
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", DISK + "/" + slug + "/index.m3u8",
                "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4], check=True)
print("  MP4 size: %dMB (>50MB HTTP bot limit)" % (os.path.getsize(mp4) // 1048576))

client = TelegramClient(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "majo_uploader_tl"),
                        API_ID, API_HASH)
client.start(bot_token=TOK)
try:
    ent = client.get_entity(CHAN); print("  channel resolved via get_entity")
except Exception as e:
    raw = (-CHAN) - 1000000000000
    ent = PeerChannel(raw); print("  get_entity failed (%s) -> PeerChannel(%d)" % (str(e)[:60], raw))
msg = client.send_file(ent, mp4, caption=(m.get("title") or slug), supports_streaming=True)
print("  UPLOADED OK -> message_id=%s" % msg.id)
client.disconnect()
os.remove(mp4)
