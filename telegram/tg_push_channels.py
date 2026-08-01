#!/usr/bin/env python3
"""
Post the Kozee TV "button-grid" message to the Telegram channel — matches the
americotv.com/tg5 template: a header image + HTML text + a grid of per-channel
buttons. Each channel button opens the MASKED player for that channel
(?ch=<opaque-id>), so the real stream address / origin is never exposed.

Config comes from the gitignored config/accounts.env (never hardcode secrets):
    TG_BOT_TOKEN=8460264810:AAF...
    TG_CHANNEL=@YourKozeeChannel        # bot must be an ADMIN of it
    TG_APP_BASE=https://stream.tv247on.com/player/tg/     # masked player (default)
    TG_IMAGE_URL=https://.../hero.jpg   # optional header image
    TG_SUBSCRIPTION_URL=https://kozeetv.com/product/subscription-package/
    TG_SUPPORT_URL=https://kozeetv.com/contact/
Run again anytime to re-post/update the message.
"""
import os, json, urllib.request

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
    return os.environ.get(k, d)

BOT  = cfg("TG_BOT_TOKEN")
CHAN = cfg("TG_CHANNEL")
BASE = cfg("TG_APP_BASE", "https://stream.tv247on.com/player/tg/")
IMG  = cfg("TG_IMAGE_URL")
SUB  = cfg("TG_SUBSCRIPTION_URL", "https://kozeetv.com/product/subscription-package/")
SUP  = cfg("TG_SUPPORT_URL", "https://kozeetv.com/")
assert BOT and CHAN, "Set TG_BOT_TOKEN and TG_CHANNEL in config/accounts.env"

# (label, opaque masked id) — id resolves to the real channel only inside nginx
CHANNELS = [
    ("Apple TV+", "a7x2k9"), ("Hulu", "b3m8p1"), ("HBO", "c5q2w7"), ("HBO Max", "d9r4t6"),
    ("HBO Max 2", "e2n8y3"), ("Paramount", "f6k1v9"), ("Paramount+", "g4h7z2"), ("Peacock", "h8b3m5"),
    ("Cinemax", "j1d6q4"), ("ESPN ACC", "k7f2x8"), ("ESPN 2", "m3p9c1"), ("ESPN U", "n5t4w6"),
    ("beIN Sports", "p2j8k3"), ("Fox Sports 1", "q9v1h7"), ("NBC Sports", "r4m6b2"), ("NBC Golf", "s8x3n5"),
]

rows = []
for i in range(0, len(CHANNELS), 4):
    rows.append([{"text": "%s ↗" % n, "url": "%s?ch=%s" % (BASE, cid)} for n, cid in CHANNELS[i:i+4]])
rows.append([{"text": "\U0001F481 Suscripción ↗", "url": SUB},
             {"text": "\U0001F937 Soporte ↗", "url": SUP}])

CAPTION = ("<b>\U0001F64B‍♂️ Mira los canales de TV más populares aquí</b>\n"
           "Puedes ver los mejores canales de EE.UU. en HD.\n\n"
           "<b>Cómo ver</b>\n"
           "Toca el canal de abajo para verlo directamente ⬇️")

def api(method, payload):
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (BOT, method),
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=25))

markup = {"inline_keyboard": rows}
# Prefer a photo header (like the template); fall back to text if the image fails.
resp = None
if IMG:
    try:
        resp = api("sendPhoto", {"chat_id": CHAN, "photo": IMG, "caption": CAPTION,
                                 "parse_mode": "HTML", "reply_markup": markup})
    except Exception as e:
        print("sendPhoto failed (%s) -> falling back to text" % str(e)[:80])
        resp = None
if not resp or not resp.get("ok"):
    resp = api("sendMessage", {"chat_id": CHAN, "text": CAPTION, "parse_mode": "HTML",
                               "reply_markup": markup, "disable_web_page_preview": True})

print("ok" if resp.get("ok") else "FAILED", "->", json.dumps(resp)[:300])
