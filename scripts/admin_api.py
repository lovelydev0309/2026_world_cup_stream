#!/usr/bin/env python3
"""Live-channel admin API (Python stdlib only — no Flask).

Routes (nginx location /admin-api/ proxies here, stripping the prefix):
  GET  /channels                       -> [{name,title,country,logo,enabled,status}, ...]
  POST /channel/<channelN>/pause       -> set enabled:false + kill the producer
  POST /channel/<channelN>/resume      -> set enabled:true (streaming-rpa relaunches it ~10s)
  GET  /accounts                       -> per-account cap/live/primaries (account_status.py)
  POST /rebalance/plan                 -> rebalance_accounts.py --dry-run, returns the plan
  POST /rebalance/apply                -> runs --apply detached (restarts the moved channels)
  GET  /rebalance/status               -> {running, log} for whichever of those is going
  POST /sendhome                       -> restart channels sitting on a non-primary account

Both write actions restart live channels, so the UI asks for the plan first and applies
only on a second, explicit click. Account data carries alias names only, never credentials.

Pause/Resume drive config/channels.json's `enabled` flag; the streaming-rpa watchdog
only relaunches enabled channels, so pausing sticks. Auth: token via `X-Admin-Token`
header or `?token=`, compared to $ADMIN_TOKEN. Binds 172.17.0.1:8090 (host-only, reached
by the nginx-rtmp container) — never exposed to the internet directly.
"""
import json, os, re, sys, time, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import account_status
import rebalance_accounts

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


# ── account balance / rebalance ────────────────────────────────────────────────
# --apply restarts one channel every STAGGER seconds, so a 6-channel move runs for the
# better part of a minute. It cannot be held open on the request, and it must not be able
# to run twice at once — a second pass while the first is still restarting would fight it
# over config/channels.json. Hence: detached child, PID in a lockfile, poll for the log.
REBAL_LOG = os.path.join(PROJECT, "logs", "rebalance_run.log")
REBAL_LOCK = os.path.join(PROJECT, "cache", "rebalance.pid")
REBAL_PY = os.path.join(PROJECT, "scripts", "rebalance_accounts.py")


def job_running():
    try:
        pid = int(open(REBAL_LOCK).read().strip())
    except Exception:
        return None
    return pid if os.path.exists("/proc/%d" % pid) else None


def job_start(argv, header):
    if job_running():
        return False
    os.makedirs(os.path.dirname(REBAL_LOCK), exist_ok=True)
    f = open(REBAL_LOG, "w")
    f.write("%s  %s\n\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), header))
    f.flush()
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=f,
                         stderr=subprocess.STDOUT, start_new_session=True, cwd=PROJECT)
    open(REBAL_LOCK, "w").write(str(p.pid))
    return True


def job_log():
    try:
        return open(REBAL_LOG).read()[-8000:]
    except Exception:
        return ""


def send_home():
    """Restart channels running on an account that is not their configured primary.

    The rebalancer cannot fix these: it reorders configured primaries, and these channels
    are already configured correctly — they simply have not reconnected since failing over.
    Only a restart re-reads source_urls[0], so that is what this does.
    """
    visiting = account_status.snapshot()["squatters"]
    if not visiting:
        return 0
    # Probe each primary before restarting onto it — see account_status.probe_primary.
    runner = (
        "import sys,time\n"
        "sys.path.insert(0,%r)\n"
        "import account_status as A, rebalance_accounts as r\n"
        "for ch in sys.argv[1:]:\n"
        "    ok,_n,note = A.probe_primary(ch)\n"
        "    if not ok:\n"
        "        print('SKIP %%s - its primary is still down (%%s)' %% (ch,note),flush=True)\n"
        "        continue\n"
        "    print('restarting %%s (primary ok: %%s)' %% (ch,note),flush=True)\n"
        "    r.restart(ch); time.sleep(%d)\n"
        "print('done',flush=True)" % (os.path.dirname(REBAL_PY),
                                      rebalance_accounts.STAGGER))
    argv = [sys.executable, "-c", runner]
    argv += [v["name"] for v in visiting]
    header = "send-home: %d channel(s) on a non-primary account -> %s" % (
        len(visiting), ", ".join("%s(%s->%s)" % (v["name"], v["now"], v["home"])
                                 for v in visiting))
    return len(visiting) if job_start(argv, header) else -1

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
        p = urlparse(self.path).path.rstrip("/")
        if p in ("/channels", "/accounts", "/rebalance/status"):
            if not self._auth(): return self._send(401, {"error": "unauthorized"})
            if p == "/channels":
                return self._send(200, listing())
            if p == "/accounts":
                return self._send(200, account_status.snapshot())
            return self._send(200, {"running": bool(job_running()), "log": job_log()})
        self._send(404, {"error": "not found"})
    def do_POST(self):
        if not self._auth(): return self._send(401, {"error": "unauthorized"})
        p = urlparse(self.path).path.rstrip("/")
        if p == "/rebalance/plan":
            # Synchronous: a dry run only re-reads the caps, no restarts, a few seconds.
            r = subprocess.run(["python3", REBAL_PY, "--dry-run"], cwd=PROJECT,
                               capture_output=True, text=True, timeout=180)
            return self._send(200, {"ok": r.returncode == 0,
                                    "plan": r.stdout + r.stderr})
        if p == "/rebalance/apply":
            started = job_start(["python3", REBAL_PY, "--apply"],
                                "rebalance --apply (restarts channels whose primary moves)")
            return self._send(200 if started else 409,
                              {"ok": started,
                               "error": None if started else "a rebalance job is already running"})
        if p == "/sendhome":
            n = send_home()
            if n == -1:
                return self._send(409, {"ok": False,
                                        "error": "a rebalance job is already running"})
            return self._send(200, {"ok": True, "channels": n})
        m = re.search(r"/channel/(channel\d+)/(pause|resume)$", urlparse(self.path).path)
        if not m: return self._send(404, {"error": "not found"})
        name, act = m.group(1), m.group(2)
        ok = pause(name) if act == "pause" else resume(name)
        self._send(200 if ok else 404, {"ok": ok, "name": name, "action": act})

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("ADMIN_TOKEN environment variable is required")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
