#!/usr/bin/env python3
"""
KozeeTV interactive menu bot  (@KozeeTVwatch_bot)  — long-polling service.

On /start (or any message) it shows the service menu, mirroring the layout of the
reference bot @kozeetvbuy_bot:

    [ 🎁 Free Trial ]  [ ⭐ Subscribe ]          <- external subscription links (unchanged)
    [ 📺 LIVE TV ] [ 🎬 Movies ] [ 🛟 Support ]   <- LIVE TV / Movies open OUR content

LIVE TV and Movies are opened as Telegram **Mini Apps** (web_app buttons, which are
allowed in private chats). That launches the player *inside* Telegram, so the real
stream host / origin is never shown as a raw address — the streams themselves are
already masked behind opaque /tv/<id>/ ids. Free Trial / Subscribe / Support are
plain URL buttons to the existing subscription site (the "blue box" — left as-is).

Everything configurable lives in the gitignored config/accounts.env (never hardcode
secrets):
    TG_BOT_TOKEN=8460264810:AAF...
    TG_LIVE_URL=https://stream.tv247on.com/player/tg/          # LIVE TV mini app
    TG_MOVIES_URL=https://stream.tv247on.com/player/vod-us/    # Movies catalog
    TG_TRIAL_URL=https://kozeetv.com/...                       # Free Trial  (blue)
    TG_SUBSCRIBE_URL=https://kozeetv.com/product/subscription-package/   # Subscribe (blue)
    TG_SUPPORT_URL=https://kozeetv.com/...                     # Support
"""
import os, json, time, urllib.request, urllib.error

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
LIVE  = cfg("TG_LIVE_URL",     "https://stream.tv247on.com/player/tg/")
MOVIE = cfg("TG_MOVIES_URL",   "https://stream.tv247on.com/player/vod-us/")
TRIAL = cfg("TG_TRIAL_URL",    "https://kozeetv.com/")
SUB   = cfg("TG_SUBSCRIBE_URL","https://kozeetv.com/product/subscription-package/")
SUP   = cfg("TG_SUPPORT_URL",  "https://kozeetv.com/")
assert BOT, "TG_BOT_TOKEN missing in config/accounts.env"
API = "https://api.telegram.org/bot%s/" % BOT

WELCOME = ("\U0001F44B <b>Welcome to Kozee TV!</b>\n"
           "Please choose your service:")

def menu_markup():
    # web_app buttons launch the player INSIDE Telegram (private chats only) so the
    # address bar never exposes our host; url buttons open the subscription site.
    return {"inline_keyboard": [
        [{"text": "\U0001F381 Free Trial", "url": TRIAL},
         {"text": "⭐ Subscribe",      "url": SUB}],
        [{"text": "\U0001F4FA LIVE TV",    "web_app": {"url": LIVE}},
         {"text": "\U0001F3AC Movies",     "web_app": {"url": MOVIE}},
         {"text": "\U0001F6DF Support",    "url": SUP}],
    ]}

def api(method, payload, timeout=40):
    req = urllib.request.Request(API + method, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        try: return json.load(e)
        except Exception: return {"ok": False, "error": "http %s" % e.code}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def log(m):
    print("[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), m), flush=True)

def send_menu(chat_id):
    r = api("sendMessage", {"chat_id": chat_id, "text": WELCOME, "parse_mode": "HTML",
                            "reply_markup": menu_markup(), "disable_web_page_preview": True})
    if r.get("ok"):
        log("menu -> chat %s OK" % chat_id)
    else:
        log("menu -> chat %s FAILED: %s" % (chat_id, json.dumps(r)[:180]))

def main():
    # /start hint + a persistent chat menu-button that also opens LIVE TV directly
    api("setMyCommands", {"commands": [{"command": "start", "description": "Open the Kozee TV menu"}]})
    api("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "LIVE TV",
                                              "web_app": {"url": LIVE}}})
    log("kozee_bot menu service up  (LIVE=%s  MOVIES=%s)" % (LIVE, MOVIE))
    offset = None
    while True:
        payload = {"timeout": 30, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        r = api("getUpdates", payload, timeout=45)
        if not r.get("ok"):
            log("getUpdates err: %s" % json.dumps(r)[:160]); time.sleep(3); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "message" in u and "chat" in u["message"]:
                    send_menu(u["message"]["chat"]["id"])
                elif "callback_query" in u:
                    cq = u["callback_query"]
                    api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    if cq.get("message"):
                        send_menu(cq["message"]["chat"]["id"])
            except Exception as e:
                log("handler err: %s" % e)

if __name__ == "__main__":
    main()
