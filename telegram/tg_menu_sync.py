#!/usr/bin/env python3
"""
Sync the KozeeTVwatch bot menu FROM the client's NocoDB (TG Shop base) into
config/tg_menu.json — which the bot re-reads on every /start. So the client edits
the menu in NocoDB (Shops.Intro + the Menu Buttons table) and it appears in the bot
automatically. Run periodically via cron.

Pulls the KOZEE TV shop's Intro (welcome text) and its active Menu Buttons, grouped
by Row_Index and ordered by Sort_Order. Kind mapping:
  trial / plans      -> the purchase bot (TG_TRIAL_URL / TG_SUBSCRIBE_URL)
  web_app|miniapp    -> in-Telegram web_app button (URL as given, or LIVE default)
  url + LIVE/Movies  -> our in-Telegram player (web_app), NOT the website
  url (other)        -> plain url button (t.me support/return, external links)

Never overwrites a good menu with a failed fetch. Secrets/config from the gitignored
config/accounts.env (NOCO_URL / NOCO_TOKEN / NOCO_* table ids / TG_* button targets).
"""
import os, json, urllib.request, urllib.error

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

NOCO   = cfg("NOCO_URL", "https://nocodb.kozeetv.com").rstrip("/")
TOK    = cfg("NOCO_TOKEN")
SHOP   = cfg("NOCO_SHOP", "KOZEE TV")
MB_T   = cfg("NOCO_MENU_TABLE", "mivq7mr4k50no69")
SHOPS_T= cfg("NOCO_SHOPS_TABLE", "mpj1ub8xma9tto6")
LIVE   = cfg("TG_LIVE_URL",      "https://stream.tv247on.com/player/tg/")
MOVIES = cfg("TG_MOVIES_URL",    "https://stream.tv247on.com/player/tg/movies.html")
TRIAL  = cfg("TG_TRIAL_URL",     "https://t.me/kozeetvbuy_bot?start=freetrial")
SUB    = cfg("TG_SUBSCRIBE_URL", "https://t.me/kozeetvbuy_bot?start=subscribe")
OUT    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "tg_menu.json")
assert TOK, "NOCO_TOKEN missing in config/accounts.env"

def api(path):
    r = urllib.request.Request(NOCO + path, headers={"xc-token": TOK})
    return json.load(urllib.request.urlopen(r, timeout=25))

def shop_of(rec):
    s = rec.get("Shops")
    return (s or {}).get("Title") if isinstance(s, dict) else None

def button(label, kind, url):
    kind = (kind or "url").strip().lower()
    low = (url or "").lower(); lab = label.lower()
    if kind == "trial":  return {"label": label, "type": "url", "value": TRIAL}
    if kind == "plans":  return {"label": label, "type": "url", "value": SUB}
    if kind in ("web_app", "miniapp", "app"):
        return {"label": label, "type": "web_app", "value": url or LIVE}
    # kind == url: route LIVE TV / Movies to our in-Telegram player; pass others through
    if "/live" in low or "live tv" in lab or "livetv" in lab:
        return {"label": label, "type": "web_app", "value": LIVE}
    if "/movie" in low or "movies" in lab or "movie" in lab:
        return {"label": label, "type": "web_app", "value": MOVIES}
    if url:
        return {"label": label, "type": "url", "value": url}
    return None

def main():
    try:
        shops = api("/api/v2/tables/%s/records?limit=200" % SHOPS_T)["list"]
        recs  = api("/api/v2/tables/%s/records?limit=500" % MB_T)["list"]
    except (urllib.error.URLError, Exception) as e:
        print("NocoDB fetch failed (%s) — keeping existing menu" % str(e)[:120]); return

    shop = next((s for s in shops if str(s.get("Title", "")).strip() == SHOP), {})
    intro = (shop.get("Intro") or "Welcome! Please choose your service").strip()

    rows_by_idx = {}
    for r in recs:
        if shop_of(r) != SHOP or not r.get("Is_Active"):
            continue
        rows_by_idx.setdefault(r.get("Row_Index") or 1, []).append(r)

    rows = []
    for ri in sorted(rows_by_idx):
        btns = []
        for b in sorted(rows_by_idx[ri], key=lambda r: r.get("Sort_Order") or 0):
            bt = button((b.get("Label") or "").strip(), b.get("Kind"), b.get("URL"))
            if bt: btns.append(bt)
        if btns: rows.append(btns)

    if not rows:
        print("no active KOZEE TV buttons found — keeping existing menu"); return
    menu = {"welcome": "<b>%s</b>" % intro, "rows": rows}
    tmp = OUT + ".tmp"; json.dump(menu, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    print("synced from NocoDB: %d row(s), %d button(s), welcome=%r"
          % (len(rows), sum(len(r) for r in rows), intro[:48]))

if __name__ == "__main__":
    main()
