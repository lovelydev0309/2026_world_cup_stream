#!/usr/bin/env python3
"""
Sync a shop's buy-bot plans FROM the client's NocoDB "Products" table into its
config/tg_plans_<shop>.json — which buy_bot.py reads for the Stars plan list + the
invoice amount. So the client sets prices in NocoDB (Products) and the buy bot
charges exactly that. Run periodically via cron, once per shop. Twin of
tg_menu_sync.py (which does the watch-bot menu).

  SHOP=kozee (default) -> NocoDB "KOZEE TV" -> config/tg_plans_kozee.json
  SHOP=majo            -> NocoDB "MAJO TV"  -> config/tg_plans_majo.json

Only Active, non-Trial products for the shop are published (trial is a separate menu
button). Never overwrites a good plan list on a failed/empty fetch. Secrets from the
gitignored config/accounts.env.
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

SHOP    = os.environ.get("SHOP", "kozee").lower()
NOCO    = cfg("NOCO_URL", "https://nocodb.kozeetv.com").rstrip("/")
TOK     = cfg("NOCO_TOKEN")
NSHOP   = cfg("NOCO_SHOP", "KOZEE TV") if SHOP == "kozee" else \
          cfg("%s_NOCO_SHOP" % SHOP.upper(), SHOP.upper() + " TV")
PROD_T  = cfg("NOCO_PRODUCTS_TABLE", "mgq4s6tsi0u17x2")
OUT     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config",
                       "tg_plans_%s.json" % SHOP)
assert TOK, "NOCO_TOKEN missing in config/accounts.env"

def api(path):
    r = urllib.request.Request(NOCO + path, headers={"xc-token": TOK})
    return json.load(urllib.request.urlopen(r, timeout=25))

def shop_of(rec):
    s = rec.get("Shop")                                   # Products links to Shops via "Shop"
    return (s or {}).get("Title") if isinstance(s, dict) else None

def main():
    try:
        recs = api("/api/v2/tables/%s/records?limit=500" % PROD_T)["list"]
    except (urllib.error.URLError, Exception) as e:
        print("[%s] NocoDB fetch failed (%s) — keeping existing plans" % (SHOP, str(e)[:100])); return

    items = [r for r in recs
             if shop_of(r) == NSHOP and r.get("Is Active") and not r.get("Is Trial")]
    items.sort(key=lambda r: ((r.get("Period Months") or 0), (r.get("Devices") or 0)))

    plans = []
    for r in items:
        months  = int(r.get("Period Months") or 0)
        devices = int(r.get("Devices") or 0)
        stars   = int(r.get("Price Stars") or 0)
        if stars <= 0:                                    # skip free/misconfigured (a ⭐0 sale)
            print("[%s] skip '%s' — Price Stars is %s" % (SHOP, r.get("Title"), r.get("Price Stars"))); continue
        plans.append({
            "id":    r.get("SKU") or ("%dm%dd" % (months, devices)),
            "label": (r.get("Title") or "%d month × %d device" % (months, devices)).strip(),
            "months": months, "devices": devices, "stars": stars,
        })

    if not plans:
        print("[%s] no active '%s' products — keeping existing plans" % (SHOP, NSHOP)); return
    tmp = OUT + ".tmp"; json.dump(plans, open(tmp, "w"), ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)
    print("[%s] synced %d plans from NocoDB '%s' Products -> %s"
          % (SHOP, len(plans), NSHOP, os.path.basename(OUT)))

if __name__ == "__main__":
    main()
