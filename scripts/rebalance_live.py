#!/usr/bin/env python3
"""Level provider connections across accounts.

Over-cap is THE systemic cause of channels flapping: the provider answers an
over-limit request with corrupt-looking or empty data, which our failover reads
as a bad feed and walks to the NEXT account — piling more load on and spreading
the failure. Total capacity is ample (110 slots for 76 channels); the problem is
distribution. For every account carrying more live channels than its
max_connections, move the excess to the emptiest account that channel can
actually use (only accounts already in its own source_urls carry its bouquet).
"""
import json, os, re, subprocess, sys, urllib.request

os.chdir("/opt/streaming-stack")
DRY = "--dry-run" in sys.argv

env = {}
for l in open("config/accounts.env"):
    l = l.strip()
    if l and not l.startswith("#") and "=" in l:
        k, v = l.split("=", 1); env[k.strip()] = v.strip()

val2alias = {}
for a, v in env.items():
    m = re.match(r"([^/]+)/([^/]+)/(.+)", v)
    if m: val2alias[(m.group(1), m.group(2))] = a

def maxconn(a):
    m = re.match(r"([^/]+)/([^/]+)/(.+)", env.get(a, ""))
    if not m: return 0
    h, u, p = m.groups()
    try:
        r = urllib.request.Request("http://%s/player_api.php?username=%s&password=%s" % (h, u, p),
                                   headers={"User-Agent": "okhttp/4.9.3"})
        return int(json.load(urllib.request.urlopen(r, timeout=20))
                   .get("user_info", {}).get("max_connections", 0))
    except Exception:
        return 0

# live assignment from the running producers (ps is truth; active_cons is laggy)
ps = subprocess.run(["ps", "-eo", "cmd"], capture_output=True, text=True).stdout
live = {}
for line in ps.splitlines():
    mc = re.search(r"hls/(channel\d+)/index\.m3u8", line)
    ms = re.search(r"http://([^/ ]+)/([^/ ]+)/([^/ ]+)/(\d+)", line)
    if mc and ms:
        live[mc.group(1)] = val2alias.get((ms.group(1), ms.group(2)), "?")

cfg = json.load(open("config/channels.json"))
by = {c["channel_name"]: c for c in cfg["channels"]}
pub = {c["name"] for c in json.load(open("player/channels.json"))}

aliases = sorted({a for a in live.values() if a != "?"} |
                 {m for c in cfg["channels"] for u in (c.get("source_urls") or [])
                  for m in re.findall(r"@@([A-Z0-9_]+)@@", u)})
cap = {a: maxconn(a) for a in aliases}
load = {a: 0 for a in aliases}
for ch, a in live.items():
    if a in load: load[a] += 1

print("account            live/max")
for a in sorted(aliases):
    print("  %-12s %d/%d%s" % (a, load.get(a, 0), cap.get(a, 0),
                               "  OVER" if load.get(a, 0) > cap.get(a, 0) else ""))

def options(ch):
    return [m for u in (by[ch].get("source_urls") or [])
            for m in re.findall(r"@@([A-Z0-9_]+)@@", u)]

moves = []
for a in sorted(aliases):
    excess = load.get(a, 0) - cap.get(a, 0)
    if excess <= 0: continue
    cands = [c for c, x in live.items() if x == a and c in pub]
    cands.sort(key=lambda c: int(re.sub(r"\D", "", c)), reverse=True)
    for ch in cands[:excess]:
        opts = [o for o in dict.fromkeys(options(ch)) if o != a and cap.get(o, 0) > 0]
        if not opts: continue
        tgt = min(opts, key=lambda o: (load.get(o, 0) - cap.get(o, 0), load.get(o, 0)))
        if load.get(tgt, 0) >= cap.get(tgt, 0): continue
        moves.append((ch, a, tgt))
        load[a] -= 1; load[tgt] = load.get(tgt, 0) + 1

print()
if not moves:
    print("no moves needed"); raise SystemExit
print("MOVES:")
for ch, frm, tgt in moves:
    print("  %-12s %s (%d/%d) -> %s (%d/%d)" % (ch, frm, load[frm], cap[frm], tgt, load[tgt], cap[tgt]))

if DRY:
    print("\n(dry run — nothing written)"); raise SystemExit

for ch, frm, tgt in moves:
    c = by[ch]
    urls = c.get("source_urls") or []
    pref = [u for u in urls if re.search(r"@@%s@@" % tgt, u)]
    rest = [u for u in urls if u not in pref]
    c["source_urls"] = pref + rest
    c["source_url"] = c["source_urls"][0]
json.dump(cfg, open("config/channels.json.tmp", "w"), ensure_ascii=False, indent=1)
os.replace("config/channels.json.tmp", "config/channels.json")
print("\nconfig written for %d channels" % len(moves))
print("RESTART:", " ".join(ch for ch, _, _ in moves))
