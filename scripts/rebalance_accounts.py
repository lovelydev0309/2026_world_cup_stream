#!/usr/bin/env python3
"""Spread channels across provider accounts so no account exceeds max_connections.

The provider caps concurrent connections per account. Over that cap it does not refuse
cleanly — it starves the stream, which looks exactly like a broken feed: short runs, the
write watchdog firing, standby drops. ch23 lost hours to this while two healthy accounts
sat idle, so getting the distribution right is worth more than any per-channel fix.

Capacity is not the problem. 110 slots for 74 channels; the failures come from clustering.

Two things are balanced, because balancing only the primaries is what left ACCT_PE1 over
cap even after its primaries were levelled:

  PRIMARY    source_urls[0] — where the channel normally connects.
  FAILOVER   the ORDER of the rest. When a channel fails it walks down this list, so if many
             channels share the same second entry a single provider blip piles them all onto
             one account and pushes it over. Each channel's remaining accounts are ordered by
             how loaded they are, so the walk heads for whoever has room.

Assignment is most-constrained-first (a channel with two possible accounts is placed before
one with six), and each channel takes the account with the lowest projected LOAD RATIO
(load/max) rather than the lowest count — the accounts differ in size (3 vs 5 slots) and
proportional filling keeps small accounts from being swamped.

It only ever REORDERS a channel's existing source_urls. It never invents an account/stream
pairing: a stream id that exists on one bouquet may not exist on another, so inventing one
would produce a dead source that looks like a provider outage.

  --dry-run   show the plan, write nothing (use this first)
  --apply     write config, regenerate the lineup, restart the channels whose primary moved
"""
import json, os, re, subprocess, sys, time, urllib.request, collections

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(PROJECT, "config", "channels.json")
ACCOUNTS = os.path.join(PROJECT, "config", "accounts.env")
GEN = os.path.join(PROJECT, "scripts", "gen_landing_json.py")
RUNNER = os.path.join(PROJECT, "scripts", "run_channel.sh")
LOGDIR = os.path.join(PROJECT, "logs")
UA = "okhttp/4.9.3"
STAGGER = 8          # seconds between restarts: one channel briefly out at a time
RESERVE = 1          # leave this many slots free per account where the options allow it.
                     # Sitting exactly AT max is not safe: when a channel fails over, the
                     # provider still holds the session it just dropped, so the account is
                     # transiently +1. Landing every account on its cap is how ACCT_PE1 went
                     # over and starved ch23. Total capacity is 110 for 74 channels, so the
                     # margin is affordable; it is best-effort and never forces an impossible
                     # move.


def load_env():
    env = {}
    for line in open(ACCOUNTS):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def max_connections(env):
    out = {}
    for a, v in env.items():
        if not a.startswith(("ACCT", "KOZEE")):
            continue
        m = re.match(r"([^/]+)/([^/]+)/(.+)", v)
        if not m:
            continue
        h, u, p = m.groups()
        try:
            req = urllib.request.Request(
                "http://%s/player_api.php?username=%s&password=%s" % (h, u, p),
                headers={"User-Agent": UA})
            d = json.load(urllib.request.urlopen(req, timeout=15)).get("user_info", {})
            out[a] = int(d.get("max_connections", 0)) or None
        except Exception:
            out[a] = None
    return out


def alias_of(url):
    m = re.search(r"@@([A-Z0-9_]+)@@", url)
    return m.group(1) if m else None


def restart(ch):
    ps = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
    for pat in (r"ffmpeg.*hls/%s/" % ch, r"run_channel\.sh %s(\s|$)" % ch):
        for line in ps.splitlines():
            if re.search(pat, line) and "grep" not in line:
                subprocess.run(["kill", "-9", line.split()[0]], capture_output=True)
    time.sleep(1)
    out = open(os.path.join(LOGDIR, "%s_stdout.log" % ch), "a")
    subprocess.Popen(["setsid", "bash", RUNNER, ch], stdin=subprocess.DEVNULL,
                     stdout=out, stderr=subprocess.STDOUT, start_new_session=True)


def main():
    apply = "--apply" in sys.argv
    if not apply and "--dry-run" not in sys.argv:
        print("pass --dry-run or --apply"); return

    env = load_env()
    mx = max_connections(env)
    cfg = json.load(open(CONFIG))

    # Only channels that actually hold a connection right now.
    chans = [c for c in cfg["channels"]
             if c.get("enabled", True) and not c.get("quarantined") and (c.get("source_urls"))]

    # Each channel's usable accounts, in the order its urls appear (dedup, order preserved).
    options = {}
    for c in chans:
        seen = []
        for u in c["source_urls"]:
            a = alias_of(u)
            if a and a not in seen and mx.get(a):
                seen.append(a)
        options[c["channel_name"]] = seen

    # INCREMENTAL, not from scratch. A clean-slate assignment balances just as well but
    # moved 39 of 74 channels, and every move is a restart — a visible gap for whoever is
    # watching. Nothing is gained by relocating a channel that is already on an account with
    # room. So: keep every current primary, then move the minimum number of channels OFF the
    # over-cap accounts until nobody is over.
    load = collections.Counter()
    primary = {}
    for c in chans:
        name = c["channel_name"]
        cur = alias_of(c["source_urls"][0])
        if cur not in options[name]:
            cur = options[name][0]
        primary[name] = cur
        load[cur] += 1

    def target(a):
        # best-effort soft cap; never below 1 or we could not place anything on a 1-slot acct
        return max(1, mx[a] - RESERVE)

    def headroom(a):
        return target(a) - load[a]

    # Two passes. The first is mandatory (get under the real cap); the second is
    # best-effort (reach the reserve target) and simply stops when no room is left.
    for limit in (lambda a: mx[a], lambda a: target(a)):
      for a in sorted(mx, key=lambda x: -(load[x] - (mx[x] or 0))):
        if not mx[a]:
            continue
        movable = sorted((c for c in chans
                          if primary[c["channel_name"]] == a
                          and len([o for o in options[c["channel_name"]] if o != a]) > 0),
                         key=lambda c: -len(options[c["channel_name"]]))
        for c in movable:
            if load[a] <= limit(a):
                break
            name = c["channel_name"]
            alts = [o for o in options[name] if o != a and headroom(o) > 0]
            if not alts:
                continue
            # proportional: fill a 5-slot account ahead of a 3-slot one
            pick = min(alts, key=lambda x: ((load[x] + 1) / float(mx[x]), -headroom(x), x))
            load[a] -= 1
            load[pick] += 1
            primary[name] = pick

    # Report + build the new url order for each channel.
    changes, plan = [], []
    for c in chans:
        name = c["channel_name"]
        opts = options.get(name) or []
        if not opts:
            continue
        want = primary[name]
        # Failover order: after the primary, head for whoever has the most room.
        rest = sorted([a for a in opts if a != want],
                      key=lambda a: (load[a] / float(mx[a]), a))
        rank = {a: i for i, a in enumerate([want] + rest)}
        new_urls = sorted(c["source_urls"],
                          key=lambda u: (rank.get(alias_of(u), 99),
                                         c["source_urls"].index(u)))
        old_primary = alias_of(c["source_urls"][0])
        if new_urls != c["source_urls"]:
            plan.append((name, old_primary, want, [alias_of(u) for u in new_urls]))
            if old_primary != want:
                changes.append((name, new_urls))
            else:
                changes.append((name, new_urls))       # order-only change, no restart needed
        c["_new_urls"] = new_urls

    print("=== projected load after rebalance ===")
    print("%-13s %-5s %-9s %s" % ("ACCOUNT", "MAX", "PRIMARIES", "STATUS"))
    bad = 0
    for a in sorted(mx):
        if not mx[a]:
            continue
        n = load[a]
        st = "OVER" if n > mx[a] else "ok"
        if n > mx[a]:
            bad += 1
        print("%-13s %-5d %-9d %s" % (a, mx[a], n, st))
    print("accounts over cap after rebalance: %d" % bad)

    moved = [p for p in plan if p[1] != p[2]]
    print("\n=== channels whose PRIMARY moves (%d) ===" % len(moved))
    for name, old, new, _ in moved:
        print("  %-12s %-12s -> %s" % (name, old, new))
    reordered = [p for p in plan if p[1] == p[2]]
    print("=== failover order only, no restart needed (%d) ===" % len(reordered))

    if not apply:
        print("\ndry run — nothing written")
        return

    for c in chans:
        if c.get("_new_urls"):
            c["source_urls"] = c["_new_urls"]
            c["source_url"] = c["_new_urls"][0]
        c.pop("_new_urls", None)
    for c in cfg["channels"]:
        c.pop("_new_urls", None)

    tmp = CONFIG + ".tmp"
    json.dump(cfg, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG)
    subprocess.run(["python3", GEN], timeout=60)
    print("\nconfig written; restarting %d channel(s) whose primary moved" % len(moved))
    for name, _, _, _ in moved:
        restart(name)
        print("  restarted %s" % name)
        time.sleep(STAGGER)
    print("done")


if __name__ == "__main__":
    main()
