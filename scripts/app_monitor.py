#!/usr/bin/env python3
"""Warn the OPS Telegram group when TV or movies stop working IN THE IPTV APPS.

Client request (2026-10-19): "To ensure that TV and movies can be played on the app, why
not establish a warning mechanism". outage_alert.py already reports channels that are down
on the website; this checks the APP path end to end, exactly as IPTV Smarters / TiviMate
use it: tv1.sazatv.com (Bunny pull zone) -> origin nginx -> xtream_gateway.py -> tvinus.com
check-vip. Each run (cron */5, flock-guarded):

  1. login       player_api.php with the dedicated monitor member -> Active
  2. catalogue   live + movie lists are non-empty
  3. live        EVERY channel as MPEG-TS (/live/u/p/<id>.ts, what Smarters plays):
                 at least MIN_TS bytes of valid TS
  4. movies      a random SAMPLE: playlist (HLS version <= 3, what VLC 2.2 inside
                 Smarters accepts) -> one segment (valid TS) + poster image.
                 A movie that fails is re-checked next run, so one bad title can alert.

Alerting is built like outage_alert.py -- informed, not spammed:
  * a problem must fail FAIL_RUNS runs in a row (10 min) before it is reported;
  * ONE message per incident; problems starting together are batched; recovery is reported;
  * a channel that is also down on the WEBSITE (live.json not LIVE) is left to
    outage_alert.py -- this raises only what is broken in the apps;
  * the monitor needs its OWN member account. If its device slot is taken (someone logged
    in with it elsewhere) that is one "monitor account in use" alert, not 45 dead channels.

Config (gitignored config/accounts.env): XTREAM_MONITOR_USER / XTREAM_MONITOR_PASS,
XTREAM_PUBLIC_HOST, and TG_OUTAGE_BOT_TOKEN / TG_OUTAGE_CHAT_ID (shared with
outage_alert.py). Writes player/app_status.json (last run, per-check result).
State: cache/app_monitor_state.json. Log: logs/app_monitor.log.
Usage: app_monitor.py [--dry-run] [--max-channels N]
"""
import json, os, random, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from outage_alert import env, send, human      # same accounts.env + OPS Telegram group

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(PROJECT, "player", "live.json")
STATUS = os.path.join(PROJECT, "player", "app_status.json")
STATE = os.path.join(PROJECT, "cache", "app_monitor_state.json")
LOG = os.path.join(PROJECT, "logs", "app_monitor.log")

UA = "VLC/2.2.4 LibVLC/2.2.4"      # what IPTV Smarters for Windows plays with
FAIL_RUNS = 2                      # consecutive failed runs before alerting (cron */5 = 10 min)
MIN_TS = 256 * 1024                # bytes of TS a working channel delivers almost at once
TS_TIMEOUT = 12                    # s to receive MIN_TS
MOVIE_SAMPLE = 4                   # random movies per run (+ last run's failures)
LARGE_SCALE = 5                    # this many problems at once => headed as a large-scale outage


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        open(LOG, "a").write(line + "\n")
    except OSError:
        pass


def fetch(url, limit=None, timeout=15):
    """-> (http status, body bytes up to limit, error text). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if limit is None:
                return r.status, r.read(), ""
            buf, deadline = b"", time.time() + timeout
            while len(buf) < limit and time.time() < deadline:
                chunk = r.read(min(65536, limit - len(buf)))
                if not chunk:
                    break
                buf += chunk
            return r.status, buf, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read()[:200], ""
    except Exception as e:
        return 0, b"", str(e)[:120]


def ts_problem(code, body, err, need):
    """None if this is a working MPEG-TS response, else a short reason."""
    if code == 403 and b"Maximum connections" in body:
        return "SLOT"                                  # handled as a monitor-account issue
    if code != 200:
        return "HTTP %s%s" % (code, (" " + err) if err else "")
    if len(body) < need:
        return "only %d KB in %ds" % (len(body) // 1024, TS_TIMEOUT)
    if body[0] != 0x47 or (len(body) > 188 and body[188] != 0x47):
        return "not MPEG-TS"
    return None


def load_json(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        json.dump(data, open(tmp, "w"), indent=1, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as e:
        log("cannot write %s: %s" % (path, e))


def run_checks(base, user, pw, max_channels, suspects):
    """-> (problems {key: (label, reason)}, report dict for app_status.json)."""
    q = "username=%s&password=%s" % (urllib.parse.quote(user), urllib.parse.quote(pw))
    up, pp = urllib.parse.quote(user, safe=""), urllib.parse.quote(pw, safe="")
    problems, report = {}, {"login": None, "live": {}, "movies": {}}

    code, body, err = fetch("%s/player_api.php?%s" % (base, q))
    try:
        info = json.loads(body)["user_info"]
    except Exception:
        info = {}
    if info.get("auth") != 1 or info.get("status") != "Active":
        why = ("HTTP %s %s" % (code, err)).strip() if code != 200 else \
              "auth=%s status=%s" % (info.get("auth"), info.get("status"))
        problems["login"] = ("App login (player_api)", why)
        report["login"] = why
        return problems, report                       # nothing else can work without it
    report["login"] = "ok"

    lists = {}
    for action in ("get_live_streams", "get_vod_streams"):
        code, body, err = fetch("%s/player_api.php?%s&action=%s" % (base, q, action), timeout=30)
        try:
            lists[action] = json.loads(body)
        except Exception:
            lists[action] = []
        if not lists[action]:
            problems["catalogue:" + action] = ("App catalogue", "%s empty (HTTP %s)" % (action, code))

    lj = load_json(LIVE, {})
    web = {c.get("name"): c.get("state") for c in lj.get("channels", [])} \
        if isinstance(lj, dict) else {}
    for c in lists["get_live_streams"][:max_channels]:
        sid, name = c["stream_id"], c["name"]
        if web and web.get("channel%d" % sid, "LIVE") != "LIVE":
            report["live"][name] = "down on website too (%s)" % web.get("channel%d" % sid)
            continue                                  # outage_alert.py owns this one
        code, body, err = fetch("%s/live/%s/%s/%d.ts" % (base, up, pp, sid),
                                limit=MIN_TS, timeout=TS_TIMEOUT)
        why = ts_problem(code, body, err, MIN_TS)
        if why == "SLOT":
            problems["slot"] = ("Monitor account", "its device slot is taken: someone is "
                                "logged in with %s elsewhere -- give the monitor its own "
                                "account" % user)
            report["live"][name] = "skipped (slot taken)"
            return problems, report
        report["live"][name] = why or "ok"
        if why:
            problems["live:%d" % sid] = ("Live: %s (ch%d)" % (name, sid), why)

    movies = lists["get_vod_streams"]
    by_id = {m["stream_id"]: m for m in movies}
    pick = [by_id[i] for i in suspects if i in by_id]
    pick += random.sample([m for m in movies if m["stream_id"] not in suspects],
                          min(MOVIE_SAMPLE, len(movies)))
    for m in pick:
        sid, name, why = m["stream_id"], m["name"], None
        code, body, err = fetch("%s/movie/%s/%s/%d.m3u8" % (base, up, pp, sid))
        segs = [l for l in body.decode("utf-8", "replace").splitlines()
                if l.startswith("http")] if code == 200 else []
        if code == 403 and b"Maximum connections" in body:
            why = "SLOT"
        elif not segs:
            why = "playlist HTTP %s %s" % (code, err) if code != 200 else "playlist has no segments"
        elif any(l.startswith("#EXT-X-VERSION:") and l.split(":")[1].strip() not in ("1", "2", "3")
                 for l in body.decode("utf-8", "replace").splitlines()):
            # IPTV Smarters for Windows plays through VLC 2.2.4, which rejects HLS > v3
            why = "playlist is HLS version > 3 (VLC 2.2 / Smarters can't play it)"
        else:
            scode, sbody, serr = fetch(segs[min(5, len(segs) - 1)], limit=64 * 1024, timeout=20)
            why = ts_problem(scode, sbody, serr, 64 * 1024)
            if why:
                why = "segment: " + why
        if not why and m.get("stream_icon"):
            pcode, pbody, perr = fetch(m["stream_icon"], limit=4096)
            if pcode != 200 or not pbody[:3] in (b"\xff\xd8\xff", b"\x89PN", b"RIF"):
                why = "poster HTTP %s" % pcode
        if why == "SLOT":
            problems["slot"] = ("Monitor account", "its device slot is taken: someone is "
                                "logged in with %s elsewhere" % user)
            break
        report["movies"][name] = why or "ok"
        if why:
            problems["movie:%d" % sid] = ("Movie: %s" % name, why)
    return problems, report


def main():
    dry = "--dry-run" in sys.argv
    max_channels = int(sys.argv[sys.argv.index("--max-channels") + 1]) \
        if "--max-channels" in sys.argv else 10 ** 6
    e = env()
    user, pw = e.get("XTREAM_MONITOR_USER"), e.get("XTREAM_MONITOR_PASS")
    host = e.get("XTREAM_PUBLIC_HOST", "tv1.sazatv.com")
    if not user or not pw:
        log("no XTREAM_MONITOR_USER / XTREAM_MONITOR_PASS in accounts.env -- not checking")
        return
    base = "http://" + host
    st = load_json(STATE, {"open": {}, "fails": {}, "suspects": []})
    t0 = time.time()
    problems, report = run_checks(base, user, pw, max_channels, st.get("suspects", []))
    took = time.time() - t0

    # consecutive-failure bookkeeping; a key absent this run has recovered
    fails = {k: st.get("fails", {}).get(k, 0) + 1 for k in problems}
    newly = [(k, problems[k]) for k in problems
             if fails[k] >= FAIL_RUNS and k not in st.get("open", {})]
    recovered = [(k, v) for k, v in st.get("open", {}).items() if k not in problems]
    opened = dict(st.get("open", {}))
    now = time.time()
    for k, (label, why) in newly:
        opened[k] = {"label": label, "why": why, "since": now}
    for k, _ in recovered:
        opened.pop(k, None)

    live_ok = sum(1 for v in report["live"].values() if v == "ok")
    log("run %.0fs: login=%s live %d/%d ok, movies %d/%d ok, problems=%s"
        % (took, report["login"], live_ok, len(report["live"]),
           sum(1 for v in report["movies"].values() if v == "ok"), len(report["movies"]),
           sorted(problems) or "none"))

    if newly:
        head = ("🚨 <b>Large-scale problem in the IPTV apps</b>" if len(newly) >= LARGE_SCALE
                else "⚠️ <b>Problem in the IPTV apps</b>")
        lines = ["%s (%s)" % (head, host), ""]
        lines += ["• %s — %s" % (label, why) for _, (label, why) in newly]
        lines += ["", "Failing for %d+ min. Website and app share the same streams; "
                  "this is what app users (Smarters/TiviMate) see." % (FAIL_RUNS * 5)]
        text = "\n".join(lines)
        log("ALERT: " + " | ".join(label for _, (label, _) in newly))
        if not dry:
            send(text)
    if recovered:
        lines = ["✅ <b>Recovered in the IPTV apps</b> (%s)" % host, ""]
        lines += ["• %s — was down %s" % (v["label"], human(now - v["since"]))
                  for _, v in recovered]
        log("RECOVERED: " + " | ".join(v["label"] for _, v in recovered))
        if not dry:
            send("\n".join(lines))

    if not dry:
        save_json(STATE, {"open": opened, "fails": fails,
                          "suspects": [int(k.split(":")[1]) for k in problems
                                       if k.startswith("movie:")]})
        save_json(STATUS, {"checked_at": int(now), "took_s": round(took), "host": host,
                           "login": report["login"], "live": report["live"],
                           "movies_sample": report["movies"],
                           "open_problems": {k: v["label"] + " — " + v["why"]
                                             for k, v in opened.items()}})


if __name__ == "__main__":
    main()
