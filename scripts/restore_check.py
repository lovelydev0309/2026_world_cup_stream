#!/usr/bin/env python3
"""Re-enable channels that were disabled for a dead source, once the provider feed
comes back healthy. A channel is a candidate if enabled=false AND its _comment_source
contains 'Re-enable when source restored'. Tests the primary source URL; on a healthy
read it flips enabled=true and regenerates the public channel list (streaming-rpa then
relaunches the producer). Idempotent — safe to run on a cron."""
import json, re, os, subprocess

BASE = "/opt/streaming-stack"
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

p = BASE + "/config/channels.json"
d = json.load(open(p)); chs = d["channels"] if isinstance(d, dict) else d
restored = []
for c in chs:
    if c.get("enabled", True):
        continue
    if "Re-enable when source restored" not in (c.get("_comment_source") or ""):
        continue
    urls = c.get("source_urls") or ([c["source_url"]] if c.get("source_url") else [])
    if urls and healthy(urls[0]):
        c["enabled"] = True
        restored.append(c["channel_name"])

if restored:
    tmp = p + ".tmp"; json.dump(d, open(tmp, "w"), ensure_ascii=False, indent=1); os.replace(tmp, p)
    subprocess.run(["python3", BASE + "/scripts/gen_landing_json.py"], timeout=60)
    import time
    print("[%s] RESTORED (feed healthy again): %s" % (time.strftime("%Y-%m-%d %H:%M"), ", ".join(restored)))
