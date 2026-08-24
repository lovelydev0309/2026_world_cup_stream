#!/usr/bin/env python3
"""Live-channel admin API (Python stdlib only — no Flask).

Routes (nginx location /admin-api/ proxies here, stripping the prefix):
  GET  /channels                       -> [{name,title,country,logo,enabled,status}, ...]
  POST /channel/<channelN>/pause       -> set enabled:false + kill the producer
  POST /channel/<channelN>/resume      -> set enabled:true (streaming-rpa relaunches it ~10s)

Pause/Resume drive config/channels.json's `enabled` flag; the streaming-rpa watchdog
only relaunches enabled channels, so pausing sticks. Auth: token via `X-Admin-Token`
header or `?token=`, compared to $ADMIN_TOKEN. Binds 172.17.0.1:8090 (host-only, reached
by the nginx-rtmp container) — never exposed to the internet directly.
"""
import json, os, re, time, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PROJECT = "/opt/streaming-stack"
CFG = os.path.join(PROJECT, "config", "channels.json")
HLS = os.path.join(PROJECT, "hls")
BASE = "https://stream.tv247on.com"
HOST = os.environ.get("ADMIN_BIND", "172.17.0.1")
PORT = int(os.environ.get("ADMIN_PORT", "8090"))
TOKEN = os.environ.get("ADMIN_TOKEN", "")

def load(): return json.load(open(CFG))
def save(d):
    tmp = CFG + ".tmp"
    json.dump(d, open(tmp, "w"), indent=2, ensure_ascii=False)
    os.replace(tmp, CFG)

def procs(name):
    """PIDs of run_channel.sh for exactly this channel (not channel1 -> channel10-19)."""
    out = []
    try:
        for pid in subprocess.run(["pgrep", "-f", "run_channel.sh %s" % name],
                                  capture_output=True, text=True).stdout.split():
            try:
                cmd = open("/proc/%s/cmdline" % pid).read().replace("\0", " ")
            except Exception:
                continue
            if re.search(r"run_channel\.sh %s( |$)" % re.escape(name), cmd):
                out.append(pid)
    except Exception:
        pass
    return out

def seg_age(name):
    d = os.path.join(HLS, name)
    try:
        newest = max((os.path.getmtime(os.path.join(d, f)) for f in os.listdir(d)
                      if f.endswith(".ts")), default=None)
    except Exception:
        newest = None
    return (time.time() - newest) if newest else None

def country(name):
    try:
        n = int("".join(filter(str.isdigit, name)))
    except ValueError:
        return ""
    return "Mexico" if n <= 15 else "Peru" if n <= 27 else "US"

def status(c):
    if not c.get("enabled", True):
        return "paused"
    a = seg_age(c["channel_name"])
    return "live" if (procs(c["channel_name"]) and a is not None and a < 30) else "down"

def listing():
    return [{"name": c["channel_name"], "title": c.get("display_name", c["channel_name"]),
             "country": c.get("country") or country(c["channel_name"]),
             "logo": "%s/player/logos/%s.png" % (BASE, c["channel_name"]),
             "enabled": c.get("enabled", True), "status": status(c)}
            for c in load().get("channels", [])]

def set_enabled(name, val):
    d = load(); ok = False
    for c in d.get("channels", []):
        if c["channel_name"] == name:
            c["enabled"] = val; ok = True
    if ok: save(d)
    return ok

def pause(name):
    if not set_enabled(name, False):
        return False
    for pid in procs(name):
        subprocess.run(["kill", "-9", pid])
    subprocess.run(["pkill", "-9", "-f", "%s/index" % name])
    return True

def resume(name):
    return set_enabled(name, True)  # streaming-rpa relaunches enabled channels

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _auth(self):
        q = parse_qs(urlparse(self.path).query)
        t = (q.get("token", [""])[0]) or self.headers.get("X-Admin-Token", "")
        return bool(TOKEN) and t == TOKEN
    def _send(self, code, obj):
        b = (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_GET(self):
        if urlparse(self.path).path.rstrip("/") == "/channels":
            if not self._auth(): return self._send(401, {"error": "unauthorized"})
            return self._send(200, listing())
        self._send(404, {"error": "not found"})
    def do_POST(self):
        if not self._auth(): return self._send(401, {"error": "unauthorized"})
        m = re.search(r"/channel/(channel\d+)/(pause|resume)$", urlparse(self.path).path)
        if not m: return self._send(404, {"error": "not found"})
        name, act = m.group(1), m.group(2)
        ok = pause(name) if act == "pause" else resume(name)
        self._send(200 if ok else 404, {"ok": ok, "name": name, "action": act})

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ADMIN_TOKEN environment variable is required")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
