#!/usr/bin/env python3
"""
Telegram Stars BUY bot (shop-aware, long-polling). Clean purchase flow:

  /start -> menu (Buy subscription / Free trial / Support / Back)   <- NO Live TV / Movies
  Buy    -> plan list (config/tg_plans_<shop>.json) -> tap plan -> Telegram Stars invoice
  pay    -> pre_checkout OK -> successful_payment -> deliver login

Telegram Stars need NO payment-provider token — just the bot token. Login/trial creation
uses the provider RESELLER API when configured (RESELLER_URL/RESELLER_KEY in accounts.env);
until then the bot records the paid order and pings the admin (TG_BUY_ADMIN_ID) to fulfill.

  SHOP=kozee -> KOZEE_BUY_BOT_TOKEN     SHOP=majo -> MAJO_BUY_BOT_TOKEN
Run:  SHOP=majo python3 telegram/buy_bot.py

NOTE: running this TAKES THE BOT OVER from its current webhook backend (api.<shop>.com).
Only start the service once that switch is intended (see deploy notes).
"""
import os, json, time, urllib.request, urllib.error

BASEDIR = os.path.dirname(os.path.abspath(__file__))
CFGDIR = os.path.join(BASEDIR, "..", "config")

def cfg(k, d=None):
    try:
        for line in open(os.path.join(CFGDIR, "accounts.env")):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                kk, vv = line.split("=", 1)
                if kk.strip() == k:
                    return vv.strip()
    except FileNotFoundError:
        pass
    return os.environ.get(k, d)

SHOP = os.environ.get("SHOP", "kozee").lower()
ES = SHOP != "kozee"                       # majo/mzolo -> Spanish, kozee -> English
def T(es, en): return es if ES else en

BOT = cfg("KOZEE_BUY_BOT_TOKEN") if SHOP == "kozee" else cfg("%s_BUY_BOT_TOKEN" % SHOP.upper())
if not BOT:
    raise SystemExit("%s buy-bot token missing in accounts.env" % SHOP)
API = "https://api.telegram.org/bot%s/" % BOT
ADMIN = cfg("TG_BUY_ADMIN_ID") or cfg("TG_ADMIN_ID")
# Support/Group -> the shop's Telegram GROUP (matches the watch bot's "Support / Group" button)
SUPPORT = "https://t.me/majotvcom" if ES else "https://t.me/+NBNp9uDma485ZDA1"
WATCH = "https://t.me/%s" % ("majotvwatch_bot" if ES else "KozeeTVwatch_bot")
# If a website subscribe page is configured for this shop, the "Subscribe" button opens
# it directly (e.g. https://majotv.com/product/suscribirse/) instead of the in-bot Stars
# plan list. Leave the key unset in accounts.env to keep the Telegram Stars flow.
SUBSCRIBE_URL = (cfg("KOZEE_SUBSCRIBE_URL", "") if SHOP == "kozee"
                 else cfg("%s_SUBSCRIBE_URL" % SHOP.upper(), "")).strip()
PLANS_FILE = os.path.join(CFGDIR, "tg_plans_%s.json" % SHOP)
MENU_FILE = os.path.join(CFGDIR, "tg_menu_%s.json" % SHOP)
ORDERS_LOG = os.path.join(BASEDIR, "..", "logs", "buy_orders_%s.jsonl" % SHOP)

def now(): return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def log(m): print("[%s] %s" % (now(), m), flush=True)

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

def load_plans():
    try: return json.load(open(PLANS_FILE))
    except Exception: return []

def banner_url():
    for fn in ("tg_menu_%s.json" % SHOP, "tg_menu.json"):
        try:
            v = (json.load(open(os.path.join(CFGDIR, fn))).get("image") or "").strip()
            if v: return v
        except Exception:
            pass
    return ""

def menu_kb():
    # Subscribe APP full-width, then Support/Group + Back side-by-side. Subscribe opens the
    # website page when SUBSCRIBE_URL is set, otherwise the in-bot Telegram Stars plans.
    sub = {"text": T("📲 Suscribirse APP", "📲 Subscribe APP")}
    if SUBSCRIBE_URL: sub["url"] = SUBSCRIBE_URL
    else:             sub["callback_data"] = "buy"
    return {"inline_keyboard": [
        [sub],
        [{"text": T("🛟 Soporte / Grupo", "🛟 Support / Group"), "url": SUPPORT},
         {"text": T("↩️ Regresar", "↩️ Back"), "url": WATCH}],
    ]}

def send_menu(chat_id):
    cap = T("👋 <b>Bienvenido</b>\nElige una opción:", "👋 <b>Welcome</b>\nChoose an option:")
    img = banner_url()
    if img:
        r = api("sendPhoto", {"chat_id": chat_id, "photo": img, "caption": cap,
                              "parse_mode": "HTML", "reply_markup": menu_kb()})
        if r.get("ok"): return
    api("sendMessage", {"chat_id": chat_id, "text": cap, "parse_mode": "HTML", "reply_markup": menu_kb()})

def send_plans(chat_id):
    plans = load_plans()
    if not plans:
        api("sendMessage", {"chat_id": chat_id, "text": T("No hay planes disponibles.", "No plans available.")})
        return
    rows = [[{"text": "%s — ⭐%d" % (p["label"], p["stars"]), "callback_data": "plan:%s" % p["id"]}] for p in plans]
    rows.append([{"text": T("⬅️ Volver", "⬅️ Back"), "callback_data": "menu"}])
    api("sendMessage", {"chat_id": chat_id, "text": T("Elige un plan:", "Choose a plan:"),
                        "reply_markup": {"inline_keyboard": rows}})

def send_invoice(chat_id, plan):
    api("sendInvoice", {
        "chat_id": chat_id, "title": plan["label"],
        "description": T("Suscripción %s. Recibirás tu usuario y contraseña tras el pago." % plan["label"],
                         "%s subscription. You'll receive your username & password after payment." % plan["label"]),
        "payload": "buy:%s:%s" % (SHOP, plan["id"]), "currency": "XTR",
        "prices": [{"label": plan["label"], "amount": int(plan["stars"])}],
    })

def reseller_line(months, devices, trial=False):
    """Create a real subscription/trial line via the provider reseller API. Returns
    {dns,user,pass,expires} or None (fall back to notify-admin). Wire exact params to
    your panel's reseller API once RESELLER_URL/RESELLER_KEY are provided."""
    url = cfg("RESELLER_URL"); key = cfg("RESELLER_KEY")
    if not (url and key):
        return None
    try:
        body = json.dumps({"key": key, "months": months, "devices": devices, "trial": trial}).encode()
        d = json.load(urllib.request.urlopen(urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json", "User-Agent": "okhttp/4.9.3"}), timeout=45))
        if d.get("username") and d.get("password"):
            return {"dns": d.get("dns") or d.get("url"), "user": d["username"],
                    "pass": d["password"], "expires": d.get("expires")}
    except Exception as e:
        log("reseller create failed: %s" % str(e)[:120])
    return None

def fmt_login(l):
    exp = (T("\nVálido hasta: %s", "\nValid until: %s") % l["expires"]) if l.get("expires") else ""
    return T("✅ <b>Tu acceso</b>\nURL: <code>%s</code>\nUsuario: <code>%s</code>\nContraseña: <code>%s</code>%s",
             "✅ <b>Your access</b>\nURL: <code>%s</code>\nUsername: <code>%s</code>\nPassword: <code>%s</code>%s") % (
        l.get("dns", ""), l.get("user", ""), l.get("pass", ""), exp)

def fulfill(chat_id, user, months, devices, tag, trial=False):
    l = reseller_line(months, devices, trial)
    if l:
        api("sendMessage", {"chat_id": chat_id, "text": fmt_login(l), "parse_mode": "HTML"})
    else:
        api("sendMessage", {"chat_id": chat_id, "text": T(
            "✅ ¡Recibido! Tu acceso se enviará en unos minutos.",
            "✅ Received! Your access will be sent within minutes.")})
        if ADMIN:
            api("sendMessage", {"chat_id": int(ADMIN), "text":
                "🔔 %s — %s — %dmo×%ddev — @%s (chat %s) — create + send login" % (
                    "TRIAL" if trial else "PAID ORDER", SHOP.upper(), months, devices, user or "?", chat_id)})
    try:
        os.makedirs(os.path.dirname(ORDERS_LOG), exist_ok=True)
        open(ORDERS_LOG, "a").write(json.dumps({"ts": time.time(), "chat": chat_id, "user": user,
            "months": months, "devices": devices, "trial": trial, "tag": tag, "auto": bool(l)}) + "\n")
    except Exception:
        pass

def main():
    api("deleteWebhook", {"drop_pending_updates": False})   # reclaim control if the old backend re-set a webhook
    api("setMyCommands", {"commands": [{"command": "start", "description": T("Menú", "Menu")}]})
    me = api("getMe", {}).get("result", {}).get("username", "?")
    log("%s BUY bot up as @%s (Telegram Stars)" % (SHOP, me))
    offset = None
    while True:
        p = {"timeout": 30, "allowed_updates": ["message", "callback_query", "pre_checkout_query"]}
        if offset is not None: p["offset"] = offset
        r = api("getUpdates", p, timeout=45)
        if not r.get("ok"):
            desc = (json.dumps(r) or "").lower()
            if "409" in desc or "webhook" in desc:   # backend re-set a webhook -> take it back
                api("deleteWebhook", {"drop_pending_updates": False}); log("reclaimed webhook (backend re-set it)")
            else:
                log("getUpdates err: %s" % json.dumps(r)[:150])
            time.sleep(3); continue
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            try:
                if "pre_checkout_query" in u:
                    api("answerPreCheckoutQuery", {"pre_checkout_query_id": u["pre_checkout_query"]["id"], "ok": True})
                elif "message" in u:
                    m = u["message"]
                    if m.get("chat", {}).get("type") != "private":
                        continue
                    chat = m["chat"]["id"]; user = m.get("from", {}).get("username", "")
                    if "successful_payment" in m:
                        pid = m["successful_payment"].get("invoice_payload", "").split(":")[-1]
                        plan = next((x for x in load_plans() if x["id"] == pid), None)
                        if plan: fulfill(chat, user, plan["months"], plan["devices"], "buy:%s" % pid)
                        else: log("paid but unknown plan id: %s" % pid)
                    else:
                        send_menu(chat)
                elif "callback_query" in u:
                    cq = u["callback_query"]; data = cq.get("data", "") or ""
                    chat = cq.get("message", {}).get("chat", {}).get("id")
                    user = cq.get("from", {}).get("username", "")
                    api("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    if not chat:
                        continue
                    if data == "buy": send_plans(chat)
                    elif data == "menu": send_menu(chat)
                    elif data == "trial": fulfill(chat, user, int(cfg("TRIAL_MONTHS", "0") or 0), 1, "trial", trial=True)
                    elif data.startswith("plan:"):
                        plan = next((x for x in load_plans() if x["id"] == data[5:]), None)
                        if plan: send_invoice(chat, plan)
            except Exception as e:
                log("handler err: %s" % e)

if __name__ == "__main__":
    main()
