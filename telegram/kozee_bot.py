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

# One codebase, multiple shops. SHOP=kozee (default) uses TG_* keys; any other shop
# (e.g. majo) uses <SHOP>_* keys — so this same file runs both the KozeeTVwatch and
# MajoTVwatch bots via separate systemd services (SHOP=majo).
SHOP = os.environ.get("SHOP", "kozee").lower()
def scfg(base, d=None):
    return cfg(("TG_%s" % base) if SHOP == "kozee" else ("%s_%s" % (SHOP.upper(), base)), d)
BOT   = scfg("BOT_TOKEN")
LIVE  = scfg("LIVE_URL",     "https://stream.tv247on.com/player/tg/")
MOVIE = scfg("MOVIES_URL",   "https://stream.tv247on.com/player/vod-us/")
TRIAL = scfg("TRIAL_URL",    "https://kozeetv.com/")
SUB   = scfg("SUBSCRIBE_URL","https://kozeetv.com/product/subscription-package/")
SUP   = scfg("SUPPORT_URL",  "https://kozeetv.com/")
assert BOT, "%s bot token missing in config/accounts.env" % SHOP
# API base is normally Telegram's cloud; a shop can point at a self-hosted local
# Bot API server (e.g. http://127.0.0.1:8081) to upload files up to 2GB.
API_BASE = scfg("API_BASE", "https://api.telegram.org").rstrip("/")
API = "%s/bot%s/" % (API_BASE, BOT)

# ── The whole menu lives in ONE editable file: config/tg_menu.json ──────────
# Shape: {"welcome": "<html text>", "rows": [[{"label","type":"url"|"web_app","value"}]]}.
# It is re-read on EVERY menu send, so edits take effect instantly (no restart).
# If the file is missing/broken the bot falls back to (and re-seeds) the defaults below.
MENU_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config",
                         "tg_menu.json" if SHOP == "kozee" else "tg_menu_%s.json" % SHOP)

def default_menu():
    return {
        "welcome": "\U0001F44B <b>Welcome to Kozee TV!</b>\nPlease choose your service:",
        "rows": [
            [{"label": "\U0001F381 Free Trial", "type": "url", "value": TRIAL},
             {"label": "⭐ Subscribe",         "type": "url", "value": SUB}],
            [{"label": "\U0001F4FA LIVE TV",    "type": "web_app", "value": LIVE},
             {"label": "\U0001F3AC Movies",     "type": "web_app", "value": MOVIE},
             {"label": "\U0001F6DF Support",    "type": "url", "value": SUP}],
        ],
    }

def load_menu():
    try:
        m = json.load(open(MENU_FILE))
        if isinstance(m, dict) and m.get("welcome") and m.get("rows"):
            return m
    except Exception:
        pass
    return default_menu()

def ensure_menu_file():
    if not os.path.exists(MENU_FILE):
        try: json.dump(default_menu(), open(MENU_FILE, "w"), ensure_ascii=False, indent=2)
        except Exception as e: log("could not seed tg_menu.json: %s" % e)

def menu_markup(m):
    # web_app buttons launch the player INSIDE Telegram (private chats only); url buttons
    # open external/subscription targets. Built from the editable config each time.
    rows = []
    for row in m.get("rows", []):
        btns = []
        for b in row:
            if not (b.get("label") and b.get("value")): continue
            t = b.get("type")
            if t == "web_app":
                btns.append({"text": b["label"], "web_app": {"url": b["value"]}})
            elif t == "callback":
                btns.append({"text": b["label"], "callback_data": b["value"]})
            else:
                btns.append({"text": b["label"], "url": b["value"]})
        if btns: rows.append(btns)
    return {"inline_keyboard": rows}

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

# ── Movies stored on Telegram: catalog config/tg_movies_<shop>.json (title+message_id).
# The bot lists ONLY these and delivers a picked one by copying it from the storage
# channel (native playback) — so movies use Telegram's storage, not our CDN.
STORAGE = cfg("TG_STORAGE_CHANNEL")
MOVIES_CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config",
                              "tg_movies_%s.json" % SHOP)
MOVIES_ES = (SHOP != "kozee")
MOVIES_TG = scfg("MOVIES_TG_URL", "https://stream.tv247on.com/player/tg-mx/movies-tg.html")

def load_movies():
    try: return json.load(open(MOVIES_CATALOG))
    except Exception: return []

def send_movie_list(chat_id):
    mv = load_movies()
    if not mv:
        api("sendMessage", {"chat_id": chat_id,
                            "text": "Aún no hay películas disponibles." if MOVIES_ES else "No movies available yet."})
        return
    head = ("🎬 <b>Películas</b> (%d) — toca una para verla:" if MOVIES_ES
            else "🎬 <b>Movies</b> (%d) — tap one to watch:") % len(mv)
    rows = [[{"text": ("🎬 %s%s" % (m.get("title") or m.get("slug"),
                                   " (%s)" % m.get("year") if m.get("year") else ""))[:60],
              "callback_data": "mv:%s" % m["message_id"]}] for m in mv[:45]]
    api("sendMessage", {"chat_id": chat_id, "text": head, "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": rows}})

def deliver_movie(chat_id, mid):
    if not STORAGE: return
    r = api("copyMessage", {"chat_id": chat_id, "from_chat_id": int(STORAGE), "message_id": int(mid)})
    if not r.get("ok"):
        log("deliver mv %s -> chat %s FAILED: %s" % (mid, chat_id, json.dumps(r)[:150]))

def send_movies_open(chat_id):
    # A reply-keyboard web_app button — the only launch that lets the Mini App call
    # sendData() back to the bot (inline/menu web_app buttons can't). Tapping a poster
    # inside then sends {mv:<message_id>} here and we deliver that film natively.
    lab = "📽️ Abrir catálogo" if MOVIES_ES else "📽️ Open catalog"
    txt = ("🎬 <b>Películas</b> — toca «%s» abajo 👇" if MOVIES_ES
           else "🎬 <b>Movies</b> — tap «%s» below 👇") % lab
    kb = {"keyboard": [[{"text": lab, "web_app": {"url": MOVIES_TG}}]],
          "resize_keyboard": True, "one_time_keyboard": True}
    api("sendMessage", {"chat_id": chat_id, "text": txt, "parse_mode": "HTML", "reply_markup": kb})

# ── App Guide / FAQ (2nd-tier sub-menu) ─────────────────────────────────────
# The 1st-tier "Guía de la app / FAQ" button (callback "faq") opens a list of
# questions; tapping one edits the message in place to show the answer, with Back
# navigation. Content is config/tg_faq_<shop>.json (Spanish for majo, English for kozee).
FAQ_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config",
                        "tg_faq.json" if SHOP == "kozee" else "tg_faq_%s.json" % SHOP)

def load_faq():
    try: return json.load(open(FAQ_FILE))
    except Exception: return None

def faq_menu_markup(faq):
    rows = [[{"text": (it.get("q") or "?")[:64], "callback_data": "faq:%d" % i}]
            for i, it in enumerate(faq.get("items", []))]
    rows.append([{"text": faq.get("back_label", "⬅️ Menu"), "callback_data": "menu"}])
    return {"inline_keyboard": rows}

def send_faq_menu(chat_id):
    faq = load_faq()
    if not faq:
        return send_menu(chat_id)
    api("sendMessage", {"chat_id": chat_id, "text": faq.get("title", "FAQ"), "parse_mode": "HTML",
                        "reply_markup": faq_menu_markup(faq), "disable_web_page_preview": True})

def edit_faq_menu(chat_id, message_id):
    faq = load_faq()
    if not faq: return
    api("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": faq.get("title", "FAQ"),
                            "parse_mode": "HTML", "reply_markup": faq_menu_markup(faq),
                            "disable_web_page_preview": True})

def edit_faq_answer(chat_id, message_id, i):
    faq = load_faq(); items = (faq or {}).get("items", [])
    if not (0 <= i < len(items)): return
    it = items[i]
    txt = "<b>%s</b>\n\n%s" % (it.get("q", ""), it.get("a", ""))
    mk = {"inline_keyboard": [[{"text": faq.get("q_back_label", "⬅️ Back"), "callback_data": "faqmenu"}],
                              [{"text": faq.get("back_label", "⬅️ Menu"), "callback_data": "menu"}]]}
    api("editMessageText", {"chat_id": chat_id, "message_id": message_id, "text": txt,
                            "parse_mode": "HTML", "reply_markup": mk, "disable_web_page_preview": True})

def send_service(chat_id, kind):
    # Deep-link landing: a single prominent web_app button for one service, so a group/
    # channel button (t.me/<bot>?start=vivo|cine) drops the user straight onto it. web_app
    # buttons are allowed here because this reply lands in the user's private chat.
    if kind == "live":
        lab = "📺 TV en Vivo" if MOVIES_ES else "📺 LIVE TV"
        txt = ("📺 <b>TV en Vivo</b> — toca el botón para ver 👇" if MOVIES_ES
               else "📺 <b>LIVE TV</b> — tap the button to watch 👇")
        url = LIVE
    else:
        lab = "🎬 Películas" if MOVIES_ES else "🎬 Movies"
        txt = ("🎬 <b>Películas</b> — toca el botón para ver 👇" if MOVIES_ES
               else "🎬 <b>Movies</b> — tap the button to watch 👇")
        url = MOVIE
    mk = {"inline_keyboard": [[{"text": lab, "web_app": {"url": url}}]]}
    r = api("sendMessage", {"chat_id": chat_id, "text": txt, "parse_mode": "HTML", "reply_markup": mk})
    if not r.get("ok"): log("service %s -> chat %s FAILED: %s" % (kind, chat_id, json.dumps(r)[:150]))

def send_menu(chat_id):
    m = load_menu()
    text = m.get("welcome", "")
    markup = menu_markup(m)
    # Optional banner above the buttons — set/clear TG_MENU_IMAGE in accounts.env
    # (read fresh each send, so it can be toggled without restarting the bot).
    img = m.get("image") or scfg("MENU_IMAGE", "")   # banner from NocoDB (synced) else config
    if img:
        r = api("sendPhoto", {"chat_id": chat_id, "photo": img, "caption": text,
                              "parse_mode": "HTML", "reply_markup": markup})
        if r.get("ok"):
            log("menu(photo) -> chat %s OK" % chat_id); return
        log("menu photo -> chat %s FAILED: %s (falling back to text)" % (chat_id, json.dumps(r)[:150]))
    r = api("sendMessage", {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                            "reply_markup": markup, "disable_web_page_preview": True})
    if r.get("ok"):
        log("menu -> chat %s OK" % chat_id)
    else:
        log("menu -> chat %s FAILED: %s" % (chat_id, json.dumps(r)[:180]))

def main():
    ensure_menu_file()   # create the shop's tg_menu file from defaults on first run
    es = (SHOP != "kozee" and scfg("LANG", "en") == "es")   # majo = Spanish labels
    start_desc = "Abrir el menú" if es else "Open the menu"
    live_lbl   = "TV en Vivo"   if es else "LIVE TV"
    # /start hint + a persistent chat menu-button that also opens LIVE TV directly
    api("setMyCommands", {"commands": [{"command": "start", "description": start_desc}]})
    api("setChatMenuButton", {"menu_button": {"type": "web_app", "text": live_lbl,
                                              "web_app": {"url": LIVE}}})
    log("%s bot menu service up  (LIVE=%s  MOVIES=%s)" % (SHOP, LIVE, MOVIE))
    offset = None
    while True:
        payload = {"timeout": 30, "allowed_updates":
                   ["message", "callback_query", "channel_post", "my_chat_member"]}
        if offset is not None:
            payload["offset"] = offset
        r = api("getUpdates", payload, timeout=45)
        if not r.get("ok"):
            log("getUpdates err: %s" % json.dumps(r)[:160]); time.sleep(3); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                # The menu uses web_app buttons, which Telegram allows ONLY in private
                # chats — sending them to a group/channel returns BUTTON_TYPE_INVALID.
                # So only ever respond in a private (DM) chat; ignore groups/channels.
                if "message" in u and "chat" in u["message"]:
                    msg = u["message"]; chat = msg["chat"]
                    if chat.get("type") == "private":
                        if "web_app_data" in msg:                       # poster tapped in the Mini App
                            try:
                                dd = json.loads(msg["web_app_data"].get("data", "{}"))
                                if dd.get("mv"): deliver_movie(chat["id"], dd["mv"])
                            except Exception as e: log("web_app_data err: %s" % e)
                        else:
                            txt = msg.get("text") or ""
                            if txt.startswith("/start") and "mv_" in txt:
                                deliver_movie(chat["id"], txt.split("mv_", 1)[1].strip())
                            elif txt.startswith("/start"):
                                parts = txt.split(maxsplit=1)
                                pl = parts[1].strip().lower() if len(parts) > 1 else ""
                                if pl in ("vivo", "live", "tv", "livetv", "envivo"):
                                    send_service(chat["id"], "live")     # group deep-link -> Live TV
                                elif pl in ("cine", "peliculas", "películas", "movies", "pelis", "vod"):
                                    send_service(chat["id"], "movies")   # group deep-link -> Movies
                                else:
                                    send_menu(chat["id"])
                            else:
                                send_menu(chat["id"])
                elif "callback_query" in u:
                    cq = u["callback_query"]
                    data = cq.get("data", "") or ""
                    m = cq.get("message"); chat = m.get("chat", {}).get("id") if m else None
                    mid_id = m.get("message_id") if m else None
                    if data == "faq" and chat:                       # open App Guide / FAQ sub-menu
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        send_faq_menu(chat)
                    elif data == "faqmenu" and chat and mid_id:       # back to the question list
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        edit_faq_menu(chat, mid_id)
                    elif data.startswith("faq:") and chat and mid_id:  # show one answer in place
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        try: edit_faq_answer(chat, mid_id, int(data.split(":", 1)[1]))
                        except Exception as e: log("faq answer err: %s" % e)
                    elif data == "menu" and chat:                     # back to the 1st-tier menu
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        send_menu(chat)
                    elif data == "movies" and chat:
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        send_movie_list(chat)   # reliable inline list -> tap -> deliver (no Mini App handoff)
                    elif data.startswith("mv:") and chat:
                        api("answerCallbackQuery", {"callback_query_id": cq["id"],
                                                    "text": "Enviando…" if MOVIES_ES else "Sending…"})
                        deliver_movie(chat, data[3:])
                    else:
                        api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                        if m and m.get("chat", {}).get("type") == "private":
                            send_menu(chat)
                elif "channel_post" in u or "my_chat_member" in u:
                    # log any channel this bot is in (used to discover the movie-storage
                    # channel id from a private invite link — bots can't resolve those).
                    obj = u.get("channel_post") or u.get("my_chat_member") or {}
                    ch = obj.get("chat", {})
                    if ch.get("type") == "channel":
                        log("CHANNEL SEEN: id=%s title=%r" % (ch.get("id"), ch.get("title")))
            except Exception as e:
                log("handler err: %s" % e)

if __name__ == "__main__":
    main()
