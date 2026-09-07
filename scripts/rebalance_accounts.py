#!/usr/bin/env python3
"""
rebalance_accounts.py — spread channels across provider accounts so none exceeds its
connection limit.

Each channel holds ONE provider connection while it is running (its current source URL).
Every account has a max_connections cap; exceeding it makes the provider serve a ~2-3s clip
and reset, which looks exactly like a dead feed and sends the channel into a failover loop.

TWO things decide how load lands on an account:

  1. PRIMARY assignment — source_urls[0], the account a healthy channel sits on. Balancing
     this alone was the original behaviour of this script.

  2. FAILOVER ORDER — source_urls[1:], where a channel goes when its primary drops. This was
     previously left in whatever order the URLs happened to be written in, so it was never
     balanced. That is how an account ends up OVER cap while its neighbours idle: on
     2026-09-07 ACCT3 (max 3) was the FIRST FALLBACK for 6 channels on top of being primary
     for 3, and ACCT_PE1 (max 5) was primary for 4 AND first fallback for 5 - so a burst of
     failures piled 7 live connections onto a 5-slot account while ACCT_PE2 sat at 1/5.
     Under churn the fallbacks matter as much as the primaries, so we now order them too.

STREAM-IDENTITY SAFETY
----------------------
A channel's source_urls can contain MORE THAN ONE stream id: the intended feed on several
accounts, plus alternate/backup feeds that are a different (often regional) variant of the
channel. Ordering purely by account capacity could promote a BACKUP id above the intended
one and silently change what viewers see. So ordering is done WITHIN each stream-id group,
and the intended id (the one the current primary uses) always stays ahead of the alternates.
Reordering never adds, drops or rewrites a URL - the set is always identical.

USAGE
    rebalance_accounts.py [--base DIR] [--dry-run]
      --base     project root (default /opt/streaming-stack; use the repo path to test locally)
      --dry-run  report the plan, write nothing
"""
import json, re, os, sys, urllib.request
from collections import defaultdict, OrderedDict

BASE = "/opt/streaming-stack"
if "--base" in sys.argv:
    BASE = sys.argv[sys.argv.index("--base") + 1]
DRY = "--dry-run" in sys.argv

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


def stream_id(u):
    return u.rstrip("/").rsplit("/", 1)[-1]


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

assigned = defaultdict(int)     # primary count per account
before = defaultdict(int)
before_fb = defaultdict(int)    # how often an account was the FIRST fallback, before
# Failover load. Under churn a first fallback is very likely to be occupied, so it must count
# almost as much as a primary when planning capacity - weighting it lightly (e.g. 1/position)
# lets the greedy pick keep choosing the same "cheap" account and CONCENTRATES load, which is
# the very failure we are fixing. Later positions decay fast because reaching them requires
# several simultaneous failures.
FB_WEIGHT = [1.0, 0.45, 0.2, 0.1]
fbload = defaultdict(float)     # failover load per account
changed = []


def load_ratio(a):
    """Worst-case occupancy of an account relative to its cap: primaries + failover load."""
    return ((assigned[a] + fbload[a]) / MAX.get(a, 5), assigned[a] + fbload[a], a)

for c in chs:
    if not c.get("enabled", True) or c.get("quarantined"): continue
    urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
    if not urls: continue
    byacct = OrderedDict()
    for u in urls:
        a = acct_of(u)
        if a: byacct.setdefault(a, []).append(u)
    if not byacct: continue
    opts = list(byacct.keys())
    before[opts[0]] += 1
    if len(opts) > 1: before_fb[opts[1]] += 1

    # ---- 1. choose the primary (unchanged behaviour: region-aware, headroom-preferring) ----
    home_region = region(opts[0])
    home = [a for a in opts if region(a) == home_region]
    headroom = [a for a in home if assigned[a] < MAX.get(a, 5) - 1]
    under_max = [a for a in home if assigned[a] < MAX.get(a, 5)]
    pool = headroom or under_max or home
    primary = min(pool, key=lambda a: (assigned[a] / MAX.get(a, 5), assigned[a]))
    assigned[primary] += 1

    # ---- 2. order the fallbacks by spare capacity, WITHIN stream-id groups ----------------
    # Preserve content intent: the id the primary uses stays first, alternates keep their
    # relative order after it. Only the ACCOUNT order inside each id group is rebalanced.
    primary_id = stream_id(byacct[primary][0])
    id_order, seen_ids = [], set()
    for u in urls:
        sid = stream_id(u)
        if sid not in seen_ids:
            seen_ids.add(sid); id_order.append(sid)
    id_order.sort(key=lambda s: 0 if s == primary_id else 1)   # intended id first, rest stable

    new, fb_pos = [], 0
    for sid in id_order:
        cands = [a for a in opts if any(stream_id(u) == sid for u in byacct[a])]
        # The primary always leads its own id group. Remaining accounts are picked GREEDILY,
        # re-evaluating load_ratio after each choice, so the first fallback of every channel
        # lands on whichever account is currently least loaded relative to its cap.
        rest = [a for a in cands if a != primary]
        ordered = ([primary] if primary in cands else [])
        while rest:
            nxt = min(rest, key=load_ratio)
            rest.remove(nxt)
            ordered.append(nxt)
            fbload[nxt] += FB_WEIGHT[min(fb_pos, len(FB_WEIGHT) - 1)]
            fb_pos += 1
        for a in ordered:
            for u in byacct[a]:
                if stream_id(u) == sid:
                    new.append(u)

    assert sorted(new) == sorted(urls), "URL set changed for %s" % c["channel_name"]
    if new != urls:
        c["source_urls"] = new
        changed.append((c["channel_name"], opts[0], primary,
                        opts[1] if len(opts) > 1 else "-", acct_of(new[1]) if len(new) > 1 else "-"))

after_fb = defaultdict(int)
for c in chs:
    if not c.get("enabled", True) or c.get("quarantined"): continue
    urls = c.get("source_urls") or []
    accs = []
    for u in urls:
        a = acct_of(u)
        if a and a not in accs: accs.append(a)
    if len(accs) > 1: after_fb[accs[1]] += 1


def sortkey(a):
    return (["Mexico", "Peru", "LATAM", "US"].index(region(a)), int(re.search(r"\d+", a).group()))


print("=== per account: PRIMARY before -> after | FIRST-FALLBACK before -> after (max) ===")
overs = []
for a in sorted(ACCTS, key=sortkey):
    b, af = before.get(a, 0), assigned.get(a, 0)
    fb, fa = before_fb.get(a, 0), after_fb.get(a, 0)
    if not (b or af or fb or fa): continue
    m = MAX.get(a, 5)
    # worst-case concurrent demand = primaries + everything that falls here first
    worst_b, worst_a = b + fb, af + fa
    flag = ""
    if af > m: flag = "  <PRIMARY OVER CAP>"; overs.append(a)
    elif worst_a > m: flag = "  (worst-case %d > max)" % worst_a
    print("  %-11s primary %d->%d | 1st-fallback %d->%d | worst-case %d->%d (max %d)%s"
          % (a, b, af, fb, fa, worst_b, worst_a, m, flag))

print("\nchanged channels: %d" % len(changed))
for cn, o, n, fo, fn in changed:
    bits = []
    if o != n: bits.append("primary %s->%s" % (o, n))
    if fo != fn: bits.append("1st-fallback %s->%s" % (fo, fn))
    print("   %-12s %s" % (cn, ", ".join(bits) or "order tightened"))

if DRY:
    print("\nDRY RUN — nothing written.")
    sys.exit(0)

tmp = BASE + "/config/channels.json.tmp"
json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1)
os.replace(tmp, BASE + "/config/channels.json")
print("\nSAVED. any account over primary cap: %s" % (overs or False))
open("/tmp/rebal_changed.txt", "w").write("\n".join(cn for cn, o, n, fo, fn in changed) + "\n")
