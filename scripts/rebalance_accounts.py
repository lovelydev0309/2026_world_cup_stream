#!/usr/bin/env python3
import json, re, os, urllib.request
from collections import defaultdict, OrderedDict

BASE = "/opt/streaming-stack"
env = {}
for line in open(BASE + "/config/accounts.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()
ACCTS = [k for k in env if re.match(r"(ACCT\d+|ACCT_PE\d+|ACCT_LAT\d+|KOZEE\d+)$", k)]
val2acct = {env[a]: a for a in ACCTS}

def acct_of(u):
    m = re.search(r"@@([A-Z0-9_]+)@@", u)
    if m: return m.group(1)
    m = re.search(r"https?://(.+?)/\d+\s*$", u)
    if m and m.group(1) in val2acct: return val2acct[m.group(1)]
    return None

def region(a):
    if a.startswith("ACCT_PE"): return "Peru"
    if a.startswith("ACCT_LAT"): return "LATAM"
    if a.startswith("KOZEE"): return "US"
    return "Mexico"

def maxc(a):
    try:
        h, u, p = env[a].split("/", 2)
        j = json.load(urllib.request.urlopen(urllib.request.Request(
            "http://%s/player_api.php?username=%s&password=%s" % (h, u, p),
            headers={"User-Agent": "okhttp/4.9.3"}), timeout=10))
        return int(j.get("user_info", {}).get("max_connections") or 0) or 5
    except Exception:
        return 5

MAX = {a: maxc(a) for a in ACCTS}
d = json.load(open(BASE + "/config/channels.json"))
chs = d["channels"] if isinstance(d, dict) else d
assigned = defaultdict(int); before = defaultdict(int); changed = []

for c in chs:
    if not c.get("enabled", True): continue
    urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
    if not urls: continue
    byacct = OrderedDict()
    for u in urls:
        a = acct_of(u)
        if a: byacct.setdefault(a, []).append(u)
    if not byacct: continue
    opts = list(byacct.keys())
    before[opts[0]] += 1
    home_region = region(opts[0])                              # keep primary in the channel's home region
    home = [a for a in opts if region(a) == home_region]
    # prefer accounts that keep 1 slot free (<= max-1) to absorb reconnect overlap;
    # fall back to filling to max only when the region is too tight for headroom.
    headroom = [a for a in home if assigned[a] < MAX.get(a, 5) - 1]
    under_max = [a for a in home if assigned[a] < MAX.get(a, 5)]
    pool = headroom or under_max or home
    primary = min(pool, key=lambda a: (assigned[a] / MAX.get(a, 5), assigned[a]))
    assigned[primary] += 1
    new = list(byacct[primary]) + [u for a in opts if a != primary for u in byacct[a]]
    if new != urls:
        c["source_urls"] = new
        changed.append((c["channel_name"], opts[0], primary))

def sortkey(a):
    return (["Mexico", "Peru", "LATAM", "US"].index(region(a)), int(re.search(r"\d+", a).group()))

print("=== primaries per account: BEFORE -> AFTER (max) ===")
for a in sorted(ACCTS, key=sortkey):
    b = before.get(a, 0); af = assigned.get(a, 0)
    if b or af:
        flag = " <OVER>" if af > MAX.get(a, 5) else ""
        print("  %-11s %d -> %d (max %d)%s" % (a, b, af, MAX.get(a, 5), flag))
print("changed channels: %d" % len(changed))
for cn, o, n in changed:
    print("   %s: %s -> %s" % (cn, o, n))
tmp = BASE + "/config/channels.json.tmp"
json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
os.replace(tmp, BASE + "/config/channels.json")
print("SAVED. any account still over max: %s" % any(assigned[a] > MAX.get(a, 5) for a in ACCTS))
# emit changed channel names for restart
open("/tmp/rebal_changed.txt", "w").write("\n".join(cn for cn, o, n in changed) + "\n")
