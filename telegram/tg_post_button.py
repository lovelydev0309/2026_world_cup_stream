#!/usr/bin/env python3
"""
Post the "Watch Live TV" button into the Kozee TV Telegram channel.
The button opens the Mini App (in-app player) — NOT a web_app button, because
web_app buttons are private-chat only; a channel post must use a plain URL button
pointing at the Mini App direct link (t.me/<bot>/<app>), which Telegram opens
in-app. Run again anytime to re-post/update the button.

Secrets are read from the gitignored config/accounts.env — NEVER hardcode them:
    TG_BOT_TOKEN=8460264810:AAF...
    TG_CHANNEL=@YourKozeeChannel      # the channel; bot must be an ADMIN of it
    TG_APP_LINK=https://t.me/KozeeTVwatch_bot/livetv   # from BotFather /newapp
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

BOT   = cfg("TG_BOT_TOKEN")
CHAN  = cfg("TG_CHANNEL")
APP   = cfg("TG_APP_LINK")
assert BOT and CHAN and APP, "Set TG_BOT_TOKEN, TG_CHANNEL, TG_APP_LINK in config/accounts.env"

payload = {
    "chat_id": CHAN,
    "text": ("\U0001F4FA *Kozee TV — En Vivo*\n"
             "16 canales de EE.UU. en HD. Toca el botón para verlos dentro de Telegram."),
    "parse_mode": "Markdown",
    # URL button (channel-post compatible) -> Telegram opens the registered Mini App in-app
    "reply_markup": {"inline_keyboard": [[
        {"text": "▶️ Abrir TV en Vivo (16 canales)", "url": APP}
    ]]},
}

req = urllib.request.Request(
    f"https://api.telegram.org/bot{BOT}/sendMessage",
    data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
resp = json.load(urllib.request.urlopen(req, timeout=20))
print("ok" if resp.get("ok") else "FAILED", "->", json.dumps(resp)[:300])
