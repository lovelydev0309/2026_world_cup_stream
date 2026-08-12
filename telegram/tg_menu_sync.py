#!/usr/bin/env python3
"""
Sync a shop's Telegram bot menu FROM the client's NocoDB (TG Shop base) into its
tg_menu file — which the bot re-reads on every /start. So the client edits the menu
in NocoDB (Shops.Intro + the Menu Buttons table) and it appears in the bot
automatically. Run periodically via cron, once per shop.

  SHOP=kozee (default) -> NocoDB "KOZEE TV" -> config/tg_menu.json      (English)
  SHOP=majo            -> NocoDB "MAJO TV"  -> config/tg_menu_majo.json (Spanish)

Kind mapping: trial/plans -> the purchase bot; url LIVE TV / Movies -> that shop's
in-Telegram web_app player; other url -> plain button. Never overwrites a good menu
on a failed fetch. Secrets/urls from the gitignored config/accounts.env.
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

SHOP = os.environ.get("SHOP", "kozee").lower()
def scfg(base, d=None):
    return cfg(("TG_%s" % base) if SHOP == "kozee" else ("%s_%s" % (SHOP.upper(), base)), d)

NOCO    = cfg("NOCO_URL", "https://nocodb.kozeetv.com").rstrip("/")
TOK     = cfg("NOCO_TOKEN")
NSHOP   = cfg("NOCO_SHOP", "KOZEE TV") if SHOP == "kozee" else scfg("NOCO_SHOP", SHOP.upper() + " TV")
MB_T    = cfg("NOCO_MENU_TABLE", "mivq7mr4k50no69")
SHOPS_T = cfg("NOCO_SHOPS_TABLE", "mpj1ub8xma9tto6")
LIVE    = scfg("LIVE_URL",      "https://stream.tv247on.com/player/tg/")
MOVIES  = scfg("MOVIES_URL",    "https://stream.tv247on.com/player/tg/movies.html")
# "web" = HLS Mini App from CDN; "telegram" = poster-catalog Mini App over Telegram-stored
# films (tapping a poster deep-links to the bot, which delivers the film natively).
MOVIES_MODE = scfg("MOVIES_MODE", "web")
MOVIES_TG   = scfg("MOVIES_TG_URL", "https://stream.tv247on.com/player/tg-mx/movies-tg.html")
TRIAL   = scfg("TRIAL_URL",     "https://t.me/kozeetvbuy_bot?start=freetrial")
SUB     = scfg("SUBSCRIBE_URL", "https://t.me/kozeetvbuy_bot?start=subscribe")
OUT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config",
                       "tg_menu.json" if SHOP == "kozee" else "tg_menu_%s.json" % SHOP)
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
    # App Guide / FAQ -> opens the 2nd-tier FAQ sub-menu (handled in-bot by callback "faq")
    if kind == "faq" or any(k in lab for k in ("faq", "guía", "guia", "app guide", "tutorial")):
        return {"label": label, "type": "callback", "value": "faq"}
    if kind in ("web_app", "miniapp", "app"):
        return {"label": label, "type": "web_app", "value": url or LIVE}
    # url kind: LIVE TV / Movies open that shop's in-Telegram player. Use the URL set in
    # NocoDB if the client entered one (so every link is visible + editable in the table),
    # otherwise fall back to the shop's configured player URL.
    if "/live" in low or "live tv" in lab or "livetv" in lab or "en vivo" in lab or "detalles" in lab:
        return {"label": label, "type": "web_app", "value": url or LIVE}
    if "/movie" in low or "/vod" in low or "movies" in lab or "movie" in lab or "pel" in lab:
        if MOVIES_MODE == "telegram":
            return {"label": label, "type": "callback", "value": "movies"}
        return {"label": label, "type": "web_app", "value": url or MOVIES}
    if url:
        return {"label": label, "type": "url", "value": url}
    return None

def resolve_banner(shop):
    """Mirror the shop's NocoDB 'Banner' attachment onto our CDN and return its public URL,
    so the client can change the menu banner just by uploading a new image in NocoDB. The
    NocoDB attachment URL is not a stable/fast host for Telegram, so we re-host it; we only
    re-download when the source changes (tracked by its path)."""
    b = shop.get("Banner")
    if not (isinstance(b, list) and b and isinstance(b[0], dict) and b[0].get("path")):
        return ""
    att = b[0]; path = att["path"]
    sub = (LIVE.rstrip("/").split("/player/")[-1] or ("tg-mx" if SHOP == "majo" else "tg")).strip("/")
    pdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "player", sub)
    ext = (att.get("title", "x.jpg").rsplit(".", 1)[-1] or "jpg").lower()
    if ext not in ("jpg", "jpeg", "png", "webp"): ext = "jpg"
    name = "hero_%s_noco.%s" % (SHOP, ext)
    dest = os.path.join(pdir, name); marker = dest + ".src"
    try: prev = open(marker).read().strip()
    except Exception: prev = ""
    if path != prev or not os.path.exists(dest):
        try:
            import urllib.parse
            src = NOCO + "/" + urllib.parse.quote(path, safe="/")   # handle non-ASCII filenames
            data = urllib.request.urlopen(urllib.request.Request(
                src, headers={"User-Agent": "curl/8"}), timeout=30).read()
            if len(data) > 1000:
                os.makedirs(pdir, exist_ok=True)
                open(dest, "wb").write(data); open(marker, "w").write(path)
                print("[%s] banner synced from NocoDB (%d KB) -> %s" % (SHOP, len(data) // 1024, name))
        except Exception as e:
            print("[%s] banner fetch failed (%s) — keeping previous" % (SHOP, str(e)[:80]))
            if not os.path.exists(dest): return ""
    return "https://stream.tv247on.com/player/%s/%s" % (sub, name)

def main():
    try:
        shops = api("/api/v2/tables/%s/records?limit=200" % SHOPS_T)["list"]
        recs  = api("/api/v2/tables/%s/records?limit=500" % MB_T)["list"]
    except (urllib.error.URLError, Exception) as e:
        print("[%s] NocoDB fetch failed (%s) — keeping existing menu" % (SHOP, str(e)[:100])); return

    shop = next((s for s in shops if str(s.get("Title", "")).strip() == NSHOP), {})
    intro = (shop.get("Intro") or "Welcome! Please choose your service").strip()

    rows_by_idx = {}
    for r in recs:
        if shop_of(r) != NSHOP or not r.get("Is_Active"):
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
        print("[%s] no active '%s' buttons — keeping existing menu" % (SHOP, NSHOP)); return
    menu = {"welcome": "<b>%s</b>" % intro, "rows": rows}
    banner = resolve_banner(shop)                 # client-editable menu image from NocoDB
    if banner: menu["image"] = banner
    tmp = OUT + ".tmp"; json.dump(menu, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    print("[%s] synced from NocoDB '%s': %d row(s), %d button(s)"
          % (SHOP, NSHOP, len(rows), sum(len(r) for r in rows)))

if __name__ == "__main__":
    main()
