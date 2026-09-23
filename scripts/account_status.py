#!/usr/bin/env python3
"""Per-provider-account connection balance.

Two numbers per account, and the gap between them is the whole point:

  PRIMARIES  how many channels are CONFIGURED to open this account first (source_urls[0]).
  LIVE       how many ffmpeg processes are ACTUALLY connected to it right now.

They drift apart whenever a channel fails over: it keeps running on somebody else's
account and stays there until something restarts it. rebalance_accounts.py only ever
reorders the CONFIGURED list, so it can honestly report "0 moves" while an account sits
over cap with visitors — which is exactly what happened to ACCT3 (4 live on a 3 cap while
ACCT6 held 1 of 3). Showing only one of these two numbers hides the real state, so the
page shows both.

LIVE is counted from /proc/<pid>/cmdline rather than the provider's active_cons: the
provider's counter lags by minutes, and `ps -eo cmd` truncates to terminal width, which
would silently undercount the long source URLs.

Caps come from the provider API (~20 accounts, a second or two in parallel) and are
cached, because they change about never and the page refreshes every few seconds.

  --refresh-caps   re-query the provider and update the cache (cron this, ~10 min)
  --json           print the snapshot that the admin API serves

NOTHING CREDENTIAL-BEARING IS EMITTED. The resolved host/user/pass is used only as a
substring to attribute a running process to an alias; only the alias name ever leaves
this module.
"""
import json, os, re, subprocess, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(PROJECT, "config", "channels.json")
ACCOUNTS = os.path.join(PROJECT, "config", "accounts.env")
CACHE = os.path.join(PROJECT, "cache", "account_caps.json")
UA = "okhttp/4.9.3"
CAPS_STALE = 3600          # caps older than this are reported as stale, still served


def load_env():
    env = {}
    try:
        for line in open(ACCOUNTS):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except IOError:
        pass
    return env


def aliases(env):
    """Provider accounts only — VOD_* and anything else in the env file is not one."""
    return {k: v for k, v in env.items()
            if k.startswith(("ACCT", "KOZEE")) and re.match(r"[^/]+/[^/]+/.+", v)}


def group_of(alias):
    if alias.startswith("KOZEE"):
        return "KOZEE"
    if alias.startswith("ACCT_PE"):
        return "Peru"
    if alias.startswith("ACCT_LAT"):
        return "LatAm"
    return "Mexico"


# ── caps (cached) ──────────────────────────────────────────────────────────────
def _probe(item):
    alias, val = item
    host, user, pw = re.match(r"([^/]+)/([^/]+)/(.+)", val).groups()
    rec = {"max": None, "active_cons": None, "status": None, "exp": None}
    try:
        req = urllib.request.Request(
            "http://%s/player_api.php?username=%s&password=%s" % (host, user, pw),
            headers={"User-Agent": UA})
        info = json.load(urllib.request.urlopen(req, timeout=15)).get("user_info", {})
        rec["max"] = int(info.get("max_connections") or 0) or None
        rec["active_cons"] = int(info.get("active_cons") or 0)
        rec["status"] = info.get("status")
        exp = info.get("exp_date")
        rec["exp"] = int(exp) if exp else None
    except Exception as e:
        rec["error"] = type(e).__name__
    return alias, rec


def refresh_caps(env=None):
    """Probe every account, then RETRY the failures one at a time.

    The provider rate-limits concurrent player_api.php calls: probing 24 accounts 8-wide
    made 8 of them return an HTTPError, and every one of those answered in 0.2s when asked
    again on its own. So the pool stays small and anything that fails gets a serial retry,
    which is both fast (~0.2s each) and what actually works.

    A cap that still cannot be read keeps its LAST KNOWN value rather than becoming null —
    caps effectively never change, and blanking them would make the page cry "unknown" over
    a transient provider hiccup.
    """
    env = env or load_env()
    acc = aliases(env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = dict(pool.map(_probe, acc.items()))
    for alias in [a for a, r in got.items() if not r.get("max")]:
        got[alias] = _probe((alias, acc[alias]))[1]

    prev = load_caps().get("accounts", {})
    for alias, rec in got.items():
        if not rec.get("max") and prev.get(alias, {}).get("max"):
            rec["max"] = prev[alias]["max"]
            rec["stale_max"] = True

    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    out = {"fetched": int(time.time()), "accounts": got}
    tmp = CACHE + ".tmp"
    json.dump(out, open(tmp, "w"), indent=1)
    os.replace(tmp, CACHE)
    return out


def load_caps():
    try:
        return json.load(open(CACHE))
    except Exception:
        return {"fetched": 0, "accounts": {}}


# ── live connections, counted from the running processes ───────────────────────
def _cmdlines():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            raw = open("/proc/%s/cmdline" % pid, "rb").read()
        except Exception:
            continue
        if raw:
            yield pid, raw.replace(b"\0", b" ").decode("utf-8", "replace")


def live_map(env):
    """channel -> alias it is CURRENTLY pulling from, per the live ffmpeg processes."""
    creds = {v: a for a, v in aliases(env).items()}
    out = {}
    for _pid, cmd in _cmdlines():
        if "ffmpeg" not in cmd:
            continue
        m = re.search(r"hls/(channel\d+)/", cmd)
        if not m:
            continue
        for val, alias in creds.items():
            if val in cmd:
                out[m.group(1)] = alias
                break
    return out


def alias_of(url):
    m = re.search(r"@@([A-Z0-9_]+)@@", url)
    return m.group(1) if m else None


def probe_primary(channel, env=None, seconds=6):
    """Is this channel's CONFIGURED primary actually serving right now?

    A channel is normally sitting on someone else's account because its own primary
    FAILED. Restarting it "home" onto a dead primary just makes it fail over again, and
    several of those at once is precisely the retry pile-up that has starved healthy
    channels sharing the account. So send-home probes first and skips what is still dead.

    curl exiting 28 (--max-time) having pulled bytes is the SUCCESS case for a live
    stream — it means we cut off a feed that was still coming. Zero bytes is the failure,
    whatever the exit code says.
    """
    env = env or load_env()
    try:
        cfg = json.load(open(CONFIG))
    except Exception:
        return False, 0, "no config"
    urls = next((c.get("source_urls") or [] for c in cfg.get("channels", [])
                 if c["channel_name"] == channel), [])
    if not urls:
        return False, 0, "no source_urls"
    url = urls[0]
    for alias, val in aliases(env).items():
        url = url.replace("@@%s@@" % alias, val)
    if "@@" in url:
        return False, 0, "unresolved alias"
    try:
        r = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(seconds),
             "-o", os.devnull, "-w", "%{http_code} %{size_download}", url],
            capture_output=True, text=True, timeout=seconds + 10)
        code, size = (r.stdout.strip().split() + ["0", "0"])[:2]
        size = int(size or 0)
        return size > 0, size, "http=%s bytes=%d" % (code, size)
    except Exception as e:
        return False, 0, type(e).__name__


def snapshot():
    env = load_env()
    acc = aliases(env)
    caps = load_caps()
    live = live_map(env)

    try:
        cfg = json.load(open(CONFIG))
    except Exception:
        cfg = {"channels": []}

    title, home = {}, {}
    for c in cfg.get("channels", []):
        name = c["channel_name"]
        title[name] = c.get("display_name", name)
        if c.get("enabled", True) and not c.get("quarantined") and c.get("source_urls"):
            home[name] = alias_of(c["source_urls"][0])

    rows, squatters = [], []
    for alias in sorted(acc, key=lambda a: (group_of(a), a)):
        rec = caps.get("accounts", {}).get(alias, {})
        here = []
        for ch, a in sorted(live.items(), key=lambda kv: int(re.sub(r"\D", "", kv[0]))):
            if a != alias:
                continue
            visiting = home.get(ch) not in (None, alias)
            here.append({"name": ch, "title": title.get(ch, ch),
                         "home": home.get(ch), "visiting": visiting})
            if visiting:
                squatters.append({"name": ch, "title": title.get(ch, ch),
                                  "home": home.get(ch), "now": alias})
        mx = rec.get("max")
        n = len(here)
        state = ("unknown" if not mx else
                 "over" if n > mx else "full" if n == mx else "ok")
        rows.append({
            "alias": alias, "group": group_of(alias), "max": mx, "live": n,
            "primaries": sum(1 for v in home.values() if v == alias),
            "state": state, "channels": here,
            "provider_active": rec.get("active_cons"),
            "provider_status": rec.get("status"), "exp": rec.get("exp"),
            "error": rec.get("error"),
        })

    known = sum(r["max"] for r in rows if r["max"])
    return {
        "generated": int(time.time()),
        "caps_fetched": caps.get("fetched", 0),
        "caps_stale": (time.time() - caps.get("fetched", 0)) > CAPS_STALE,
        "accounts": rows,
        "squatters": squatters,
        "totals": {"accounts": len(rows), "slots": known,
                   "live": sum(r["live"] for r in rows),
                   "over": sum(1 for r in rows if r["state"] == "over")},
    }


if __name__ == "__main__":
    if "--refresh-caps" in sys.argv:
        d = refresh_caps()
        ok = sum(1 for r in d["accounts"].values() if r.get("max"))
        print("caps refreshed: %d/%d accounts answered" % (ok, len(d["accounts"])))
    if "--json" in sys.argv or "--refresh-caps" not in sys.argv:
        print(json.dumps(snapshot(), indent=1))
