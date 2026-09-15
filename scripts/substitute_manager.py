#!/usr/bin/env python3
"""Air a stand-in channel when the provider has been down for hours, and put the real one
back by itself the moment the feed returns.

Client rule (2026-09-14): "If a channel is unavailable for five hours due to an issue with
the service provider, use a different channel until it is restored."

A dead channel currently shows our standby slate — a silent holding card — for as long as the
provider stays down, which for GUATEVISION has already been days. This airs a working
substitute instead, and keeps probing the original so the swap reverses itself.

  DEAD  -> (>= DEAD_HOURS of every source failing) -> SUBSTITUTED
  SUBSTITUTED -> (RESTORE_PROBES consecutive healthy probes of the ORIGINAL) -> back to normal

Deliberate design choices:

* The swap is done by moving `source_urls` aside into `_original_source_urls` and putting the
  substitute's urls in their place. run_channel.sh therefore needs NO changes — it just reads
  source_urls as always. run_channel.sh is the most safety-critical file in the stack and is
  not worth touching for this.
* Only a PROVIDER-side failure counts. Every source must fail, and we require at least one
  probe to have gone to an account with a free connection slot — otherwise a fleet-wide
  connection-cap squeeze would look exactly like a dead feed and we would swap out healthy
  channels wholesale. See the connection-limit ceiling notes.
* A substituted channel is LABELLED in the published lineup (gen_landing_json reads the
  `substituted` marker). If a stand-in were silently passed off as the real channel, nobody
  would ever chase the provider and the client would not know what their viewers are seeing.
  Set "label_substitutes": false in the config block to turn that off.
* The original is re-probed every run; RESTORE_PROBES consecutive good reads are required
  before switching back, so an intermittent feed cannot flap the channel in and out.

Config, per channel in config/channels.json:

    "substitute": {"source_urls": ["http://@@ACCT_LAT9@@/30676"], "label": "Univision"}

Run from cron every 10 min:
    */10 * * * * flock -n /tmp/substitute.lock python3 /opt/streaming-stack/scripts/substitute_manager.py
"""
import json, os, re, subprocess, sys, time, urllib.request

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(PROJECT, "config", "channels.json")
ACCOUNTS = os.path.join(PROJECT, "config", "accounts.env")
STATE = os.path.join(PROJECT, "cache", "substitute_state.json")
LOG = os.path.join(PROJECT, "logs", "substitute_manager.log")
GEN = os.path.join(PROJECT, "scripts", "gen_landing_json.py")
RUNNER = os.path.join(PROJECT, "scripts", "run_channel.sh")
LOGDIR = os.path.join(PROJECT, "logs")

DEAD_HOURS = 5           # client rule: five hours down before a stand-in goes on
PROBE_SECS = 8           # per-source pull; a live feed delivers MBs in this time
PROBE_MIN_BYTES = 1_200_000  # ~150 KB/s over PROBE_SECS. NOT just "some bytes arrived":
                             # ch23 Peru Magico trickled 851 KB in 25s (22 of 25 seconds idle,
                             # ~34 KB/s) which is unwatchable but sailed past the old 300 KB bar,
                             # so a starved feed read as healthy and never qualified for a
                             # stand-in. Healthy feeds here deliver 375-1250 KB/s, so this
                             # separates them cleanly from a trickle.
RESTORE_PROBES = 6       # consecutive healthy reads of the ORIGINAL before switching back.
                         # Was 3 (~30 min at the 10-min cron). ch23's feed recovered just long
                         # enough to clear that bar, took its channel back, then died again --
                         # so viewers returned to the standby slate AND the full 5h wait
                         # restarted. 6 (~1h sustained) is far harder to clear by luck.
REPEAT_DEAD_HOURS = 0.5  # a channel that has ALREADY needed a stand-in has proven its feed
                         # unreliable, so making viewers watch a holding card for another five
                         # hours serves nobody. The first outage honours the client's 5h rule;
                         # every later one swaps back in after 30 min.
UA = "okhttp/4.9.3"      # the provider is UA-filtered


def now():
    return time.time()


def stamp(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t or now()))


def log(msg):
    line = "[%s] %s" % (stamp(), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_env():
    env = {}
    try:
        for line in open(ACCOUNTS):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


ENV = load_env()


def alias_of(url):
    m = re.search(r"@@([A-Z0-9_]+)@@", url)
    return m.group(1) if m else None


def resolve(url):
    a = alias_of(url)
    return url.replace("@@%s@@" % a, ENV.get(a, "?")) if a else url


def free_slots():
    """active vs max per account, so we can tell a dead feed from a capped account."""
    out = {}
    for a, v in ENV.items():
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
            out[a] = int(d.get("max_connections", 0)) - int(d.get("active_cons", 0))
        except Exception:
            out[a] = None          # unknown -- treated as "cannot vouch for capacity"
    return out


def probe(url):
    """True if this source delivers real data. rc=28 (our own --max-time) is EXPECTED on a
    live stream and is a SUCCESS as long as bytes arrived."""
    try:
        r = subprocess.run(
            ["curl", "-s", "-A", UA, "-L", "--max-time", str(PROBE_SECS),
             "-o", "/dev/null", "-w", "%{http_code} %{size_download}", resolve(url)],
            capture_output=True, text=True, timeout=PROBE_SECS + 6)
        parts = r.stdout.strip().split()
        if len(parts) < 2:
            return False
        return parts[0] == "200" and int(parts[1]) >= PROBE_MIN_BYTES
    except Exception:
        return False


def any_alive(urls, slots):
    """(alive, trustworthy). trustworthy is False when every probe went to an account we
    could not confirm had a spare slot -- then a failure is not proof the FEED is dead."""
    trustworthy = False
    for u in urls:
        a = alias_of(u)
        if slots.get(a) is not None and slots.get(a, 0) > 0:
            trustworthy = True
        if probe(u):
            return True, True
    return False, trustworthy


def restart(ch):
    """Bounce the producer so it re-reads source_urls. The wrapper caches its ffmpeg args,
    so killing ffmpeg alone would keep the OLD source."""
    for pat in (r"[f]fmpeg.*hls/%s/" % ch, r"[r]un_channel\.sh %s( |$)" % ch):
        try:
            ps = subprocess.run(["ps", "-eo", "pid,cmd"], capture_output=True, text=True).stdout
            for line in ps.splitlines():
                if re.search(pat, line):
                    pid = line.split()[0]
                    subprocess.run(["kill", "-9", pid], capture_output=True)
        except Exception:
            pass
    time.sleep(1)
    try:
        out = open(os.path.join(LOGDIR, "%s_stdout.log" % ch), "a")
        subprocess.Popen(["setsid", "bash", RUNNER, ch],
                         stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         start_new_session=True)
    except Exception as e:
        log("  could not relaunch %s: %s" % (ch, str(e)[:80]))


def save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def main():
    dry = "--dry-run" in sys.argv
    cfg = json.load(open(CONFIG))
    try:
        state = json.load(open(STATE))
    except Exception:
        state = {}

    slots = free_slots()
    changed, restarts = False, []

    if "--status" in sys.argv:
        for c in cfg["channels"]:
            s = state.get(c["channel_name"], {})
            if c.get("substituted") or s.get("dead_since"):
                print("  %-12s %-26s substituted=%s dead_since=%s good=%s"
                      % (c["channel_name"], str(c.get("display_name"))[:26],
                         bool(c.get("substituted")),
                         stamp(s["dead_since"]) if s.get("dead_since") else "-",
                         s.get("good", 0)))
        return

    for c in cfg["channels"]:
        ch = c["channel_name"]
        sub = c.get("substitute")
        if not sub or not sub.get("source_urls"):
            continue
        st = state.setdefault(ch, {})
        active = bool(c.get("substituted"))
        originals = c.get("_original_source_urls") if active else (c.get("source_urls") or [])

        alive, trustworthy = any_alive(originals, slots)

        if alive:
            st["dead_since"] = None
            st["good"] = st.get("good", 0) + 1 if active else 0
            if active and st["good"] >= RESTORE_PROBES:
                # Provider is back and has stayed back -- restore the real channel.
                c["source_urls"] = originals
                c["source_url"] = originals[0]
                c.pop("_original_source_urls", None)
                c.pop("substituted", None)
                c["enabled"] = True
                st["good"] = 0
                changed = True
                restarts.append(ch)
                log("RESTORED %s -- original feed healthy %d probes running" % (ch, RESTORE_PROBES))
            continue

        # Not alive. Only act if the failure is provably the provider's.
        st["good"] = 0
        if not trustworthy:
            log("%s: all sources failed but no account had a confirmed free slot -- "
                "treating as capacity, NOT swapping" % ch)
            continue
        if active:
            continue                                   # already on the stand-in
        if not st.get("dead_since"):
            st["dead_since"] = now()
            log("%s: provider feed down, clock started (swap in %.1fh)" % (ch, DEAD_HOURS))
            continue

        down_h = (now() - st["dead_since"]) / 3600.0
        need = DEAD_HOURS if not st.get("fails") else REPEAT_DEAD_HOURS
        if down_h < need:
            tag = ""
            if st.get("fails"):
                tag = " (repeat outage #%d)" % st["fails"]
            log("%s: down %.1fh of %.1fh%s" % (ch, down_h, need, tag))
            continue

        # Five hours down -> put the stand-in on air.
        if not any_alive(sub["source_urls"], slots)[0]:
            log("%s: down %.1fh but the substitute is not alive either -- leaving as is"
                % (ch, down_h))
            continue
        c["_original_source_urls"] = originals
        c["source_urls"] = list(sub["source_urls"])
        c["source_url"] = sub["source_urls"][0]
        st["fails"] = st.get("fails", 0) + 1
        c["substituted"] = {"since": stamp(), "label": sub.get("label", "alternate channel"),
                            "after_hours": round(down_h, 1), "outage": st["fails"]}
        c["enabled"] = True
        changed = True
        restarts.append(ch)
        log("SUBSTITUTED %s -> %s after %.1fh down" % (ch, sub.get("label"), down_h))

    if dry:
        log("dry run -- no changes written")
        return

    save(STATE, state)
    if changed:
        save(CONFIG, cfg)
        subprocess.run(["python3", GEN], timeout=60)
        for ch in restarts:
            restart(ch)
        log("applied: %d channel(s) changed" % len(restarts))


if __name__ == "__main__":
    main()
