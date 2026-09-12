#!/usr/bin/env python3
"""Re-enable channels that were disabled for a dead source, once the provider feed
comes back healthy. A channel is a candidate if enabled=false AND its _comment_source
contains 'Re-enable when source restored'. Tests the primary source URL.

A single healthy read is NOT enough: an intermittent feed (e.g. Panamericana) passes one
lucky probe, gets re-enabled, then flaps for days — which is worse for viewers than staying
hidden, and it burns provider connection slots that the healthy channels need. So a channel
must read healthy on RESTORE_STREAK consecutive runs (hourly cron => 2h of sustained health)
before it is flipped back on. The streak lives in cache/restore_streak.json, not the config,
so the config stays clean; any bad read resets it to zero.

Idempotent — safe to run on a cron."""
import json, re, os, subprocess, time

BASE = "/opt/streaming-stack"
STREAK_FILE = BASE + "/cache/restore_streak.json"
RESTORE_STREAK = 2          # consecutive healthy hourly reads required before re-enabling

env = {}
for line in open(BASE + "/config/accounts.env"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k.strip()] = v.strip()

def resolve(u):
    m = re.search(r"@@([A-Z0-9_]+)@@", u)
    return u.replace("@@%s@@" % m.group(1), env.get(m.group(1), "?")) if m else u

def healthy(u):
    try:
        r = subprocess.run(["curl", "-s", "-A", "okhttp/4.9.3", "-L", "--max-time", "10",
                            "-o", "/dev/null", "-w", "%{http_code} %{size_download}", resolve(u)],
                           capture_output=True, text=True, timeout=15)
        code, size = r.stdout.strip().split()
        return code == "200" and int(size) > 500000
    except Exception:
        return False

def load_streaks():
    try:
        return json.load(open(STREAK_FILE))
    except Exception:
        return {}

def save_streaks(s):
    try:
        os.makedirs(os.path.dirname(STREAK_FILE), exist_ok=True)
        tmp = STREAK_FILE + ".tmp"
        json.dump(s, open(tmp, "w"), indent=1)
        os.replace(tmp, STREAK_FILE)
    except Exception:
        pass

p = BASE + "/config/channels.json"
d = json.load(open(p)); chs = d["channels"] if isinstance(d, dict) else d
streaks = load_streaks()
restored, pending = [], []
seen = set()

for c in chs:
    if c.get("enabled", True):
        continue
    if "Re-enable when source restored" not in (c.get("_comment_source") or ""):
        continue
    name = c["channel_name"]
    seen.add(name)
    urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
    if urls and healthy(urls[0]):
        n = streaks.get(name, 0) + 1
        if n >= RESTORE_STREAK:
            c["enabled"] = True
            streaks.pop(name, None)
            restored.append(name)
        else:
            streaks[name] = n
            pending.append("%s (%d/%d)" % (name, n, RESTORE_STREAK))
    else:
        if streaks.pop(name, None):
            pending.append("%s (reset — probe failed)" % name)

for gone in [k for k in streaks if k not in seen]:   # no longer a candidate
    streaks.pop(gone, None)
save_streaks(streaks)

if restored:
    tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, p)
    subprocess.run(["python3", BASE + "/scripts/gen_landing_json.py"], timeout=60)
    print("[%s] RESTORED (feed healthy %d runs running): %s"
          % (time.strftime("%Y-%m-%d %H:%M"), RESTORE_STREAK, ", ".join(restored)))
if pending:
    print("[%s] still proving: %s" % (time.strftime("%Y-%m-%d %H:%M"), ", ".join(pending)))
