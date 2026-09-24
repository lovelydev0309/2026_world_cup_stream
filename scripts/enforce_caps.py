#!/usr/bin/env python3
"""Keep every provider account at or under max_connections, continuously.

WHY THIS EXISTS (measured 2026-09-24): three accounts were over cap at once —
ACCT_PE1 at 10/5, ACCT3 and ACCT5 at 4/3 — while ACCT6 sat at 0/3 and KOZEE1 at 0/5.
Nothing in the stack was watching live occupancy:

  * run_channel.sh fails over with `URL_IDX=(URL_IDX+1) % NUM_URLS` — a blind step to the
    next configured source. It never asks whether that account has room.
  * A channel that lands on a backup and runs fine STAYS there (run_channel.sh's own
    comment says so). Only the 6h drift-reset or a standby cycle sends it home, so the
    live distribution decays away from the balanced primaries all day.
  * rebalance_accounts.py only reorders CONFIG. It cannot move a running channel, so it
    reports "0 moves" while accounts are over cap.

Over cap the provider does not refuse cleanly — it starves the stream, which looks exactly
like a broken feed. So every channel on an over-cap account degrades, they all fail
together, and they all step to the next account together. One over-cap account manufactures
the next one.

WHAT THIS DOES: counts LIVE connections per account, and for each account over its cap
moves the excess to the reachable account with the most free slots — the emptiest first.
Moving means rewriting that channel's source_urls so the target is primary and restarting
it, because only a restart re-reads source_urls[0].

  --dry-run   print the plan, change nothing
  --apply     write config and restart the moved channels

Two guards that are not optional:

  PROBE FIRST   The target is probed before a channel is sent there. ACCT_PE2 shows
                max=5/status=Active on player_api.php but answers 401 on its stream URLs;
                moving channels onto it would take them off air for nothing.
  BUDGET        At most MAX_MOVES per run, staggered. Each move is a brief outage, and a
                burst of restarts is itself a way to push an account over cap.
"""
import collections, json, os, re, subprocess, sys, time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT, "scripts"))
import account_status as A
import rebalance_accounts as R

CONFIG = os.path.join(PROJECT, "config", "channels.json")
GEN = os.path.join(PROJECT, "scripts", "gen_landing_json.py")
STATE = os.path.join(PROJECT, "cache", "enforce_caps.json")
MAX_MOVES = 6        # per run; cron re-runs, so a big imbalance converges over a few passes
STAGGER = 8          # seconds between restarts — one channel briefly out at a time
PROBE_SECS = 5
COOLDOWN = 600       # do not move the same channel again within this many seconds


def proc_age(pid):
    """Seconds since the process started (/proc/<pid> ctime is close enough)."""
    try:
        return time.time() - os.stat("/proc/%s" % pid).st_ctime
    except Exception:
        return 1e9


def live_detail(env):
    """channel -> (alias, age_seconds), from the running ffmpeg processes."""
    creds = {v: a for a, v in A.aliases(env).items()}
    out = {}
    for pid, cmd in A._cmdlines():
        if "ffmpeg" not in cmd:
            continue
        m = re.search(r"hls/(channel\d+)/", cmd)
        if not m:
            continue
        for val, alias in creds.items():
            if val in cmd:
                out[m.group(1)] = (alias, proc_age(pid))
                break
    return out


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {}


def save_state(d):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1)
    os.replace(tmp, STATE)


def main():
    apply = "--apply" in sys.argv
    if not apply and "--dry-run" not in sys.argv:
        print("pass --dry-run or --apply")
        return 2

    env = A.load_env()
    # Only ACTIVE accounts have usable slots. An expired subscription still reports
    # max_connections=5 and would look like a perfect destination while 401-ing every
    # stream, so it must never be a move target or count as capacity.
    caps = {a: r.get("max") for a, r in A.load_caps().get("accounts", {}).items()
            if str(r.get("status") or "").strip().lower() == "active"}
    live = live_detail(env)
    cfg = json.load(open(CONFIG))
    by_name = {c["channel_name"]: c for c in cfg["channels"]}

    count = collections.Counter(v[0] for v in live.values())
    reach = {}      # channel -> [aliases it can actually use, in configured order]
    home = {}
    for name, c in by_name.items():
        if not (c.get("enabled", True) and not c.get("quarantined") and c.get("source_urls")):
            continue
        seen = []
        for u in c["source_urls"]:
            a = R.alias_of(u)
            if a and a not in seen and caps.get(a):
                seen.append(a)
        reach[name] = seen
        if seen:
            home[name] = seen[0]

    over = {a: count[a] - caps[a] for a in caps
            if caps.get(a) and count[a] > caps[a]}
    print("=== live occupancy ===")
    for a in sorted(caps):
        if not caps[a]:
            continue
        n = count[a]
        mark = "  OVER by %d" % (n - caps[a]) if n > caps[a] else (
               "  empty" if n == 0 else "")
        print("  %-13s %d/%-3d%s" % (a, n, caps[a], mark))
    if not over:
        print("\nnothing over cap — no moves needed")
        return 0

    state = load_state()
    now = time.time()
    moves, unresolved = [], []

    for acct in sorted(over, key=lambda a: -over[a]):
        need = over[acct]
        # Evict VISITORS before residents (a visitor is already off its primary, so moving
        # it costs nothing in correctness), then newest arrival first — the most recent
        # connection is the one that pushed this account over.
        here = [(ch, age) for ch, (al, age) in live.items() if al == acct]
        here.sort(key=lambda t: (home.get(t[0]) == acct, t[1]))
        for ch, age in here:
            if need <= 0 or len(moves) >= MAX_MOVES:
                break
            if now - state.get(ch, {}).get("moved_at", 0) < COOLDOWN:
                continue
            opts = [a for a in reach.get(ch, [])
                    if a != acct and caps.get(a) and (caps[a] - count[a]) > 0]
            # "emptiest or most remaining capacity first", exactly as asked
            opts.sort(key=lambda a: (-(caps[a] - count[a]), count[a], a))
            placed = False
            for t in opts:
                ok, nbytes, note = probe_target(ch, t, env)
                if not ok:
                    print("  skip target %-12s for %-12s (%s)" % (t, ch, note))
                    continue
                moves.append({"channel": ch, "from": acct, "to": t,
                              "free_at_target": caps[t] - count[t], "probe": note,
                              "visiting": home.get(ch) != acct})
                count[acct] -= 1
                count[t] += 1
                need -= 1
                placed = True
                break
            if not placed:
                unresolved.append((ch, acct, "no reachable target with room that answers"))
        if need > 0:
            unresolved.append((None, acct, "still over by %d" % need))

    print("\n=== plan (%d move%s) ===" % (len(moves), "" if len(moves) == 1 else "s"))
    for m in moves:
        print("  %-12s %-12s -> %-12s (target had %d free)%s" % (
            m["channel"], m["from"], m["to"], m["free_at_target"],
            "  [was visiting]" if m["visiting"] else ""))
    for ch, acct, why in unresolved:
        print("  UNRESOLVED %-12s %s" % (ch or acct, why))

    if not apply:
        print("\ndry run — nothing written")
        return 0
    if not moves:
        return 0

    # Put the target first in source_urls so the restart lands there and STAYS there.
    for m in moves:
        c = by_name[m["channel"]]
        urls = c["source_urls"]
        first = [u for u in urls if R.alias_of(u) == m["to"]]
        c["source_urls"] = first + [u for u in urls if u not in first]
        c["source_url"] = c["source_urls"][0]
    tmp = CONFIG + ".tmp"
    json.dump(cfg, open(tmp, "w"), ensure_ascii=False, indent=1)
    os.replace(tmp, CONFIG)
    subprocess.run(["python3", GEN], timeout=60)

    for m in moves:
        R.restart(m["channel"])
        state[m["channel"]] = {"moved_at": time.time(), "to": m["to"]}
        save_state(state)
        print("  moved %s -> %s" % (m["channel"], m["to"]))
        time.sleep(STAGGER)
    print("done")
    return 0


def probe_target(channel, alias, env):
    """Would this channel's source ON THIS ACCOUNT actually serve? Bytes or it doesn't."""
    cfg = json.load(open(CONFIG))
    c = next((x for x in cfg["channels"] if x["channel_name"] == channel), None)
    if not c:
        return False, 0, "no config"
    url = next((u for u in c.get("source_urls") or [] if R.alias_of(u) == alias), None)
    if not url:
        return False, 0, "no url for %s" % alias
    for a, v in A.aliases(env).items():
        url = url.replace("@@%s@@" % a, v)
    if "@@" in url:
        return False, 0, "unresolved alias"
    try:
        r = subprocess.run(
            ["curl", "-s", "-A", A.UA, "-L", "--max-time", str(PROBE_SECS),
             "-o", os.devnull, "-w", "%{http_code} %{size_download}", url],
            capture_output=True, text=True, timeout=PROBE_SECS + 10)
        code, size = (r.stdout.strip().split() + ["0", "0"])[:2]
        size = int(size or 0)
        return size > 0, size, "http=%s bytes=%d" % (code, size)
    except Exception as e:
        return False, 0, type(e).__name__


if __name__ == "__main__":
    sys.exit(main())
