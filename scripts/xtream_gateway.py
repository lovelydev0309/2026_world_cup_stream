#!/usr/bin/env python3
"""Xtream-Codes-compatible gateway — the "DNS" external IPTV apps log in to.

TViNUS reseller members (WordPress users on tvinus.com) enter this server's address plus
their panel username/password in TiviMate / IPTV Smarters / XCIPTV and get our live
lineup + VOD, exactly like a line from a code panel. Python stdlib only (like admin_api).

Routes (nginx server block for the DNS host proxies everything here):
  GET /player_api.php?username&password[&action=...]   user_info/server_info + catalogue
  GET /get.php?username&password[&type=m3u_plus]        M3U playlist
  GET /xmltv.php                                        empty guide (apps want a 200)
  GET /live/<u>/<p>/<id>.<ext>  and  /<u>/<p>/<id>      live playlist, segments on the CDN
  GET /movie/<u>/<p>/<id>.<ext>                         302 -> signed /xv/ VOD path
  GET /xv/<user>/<exp>/<sig>/<key>/<file>               VOD playlist/segment from disk

Auth = tvinus-reseller REST `auth/check-vip` (POST username+password ->
{user_id,is_vip,authorized,points,expires_at}). check-vip did NOT verify the password
(2026-10-16: any password, even empty, got the full VIP answer for a real username), so
a login is accepted only when the response explicitly confirms the password
(TVINUS_PW_FIELD, default `password_valid: true`, the contract asked of ASSE). Until the
panel sends it, every login fails closed. Results are cached so a channel flip
costs no WordPress round trip, and a tvinus.com outage keeps already-active viewers on
air from the stale cache instead of kicking everyone off.

Device limits: each member's max_connections comes from check-vip (per plan — 1 or 2
devices). The gateway serves every live playlist itself (re-read from local disk, segment
URIs made absolute to the CDN, so video bytes still come from the CDN) and every VOD file,
so each poll proves a device is still watching. A device is ip + User-Agent; a NEW device
is refused while the plan's slots are taken, a device already watching is never cut off,
and a device that stops frees its slot after SESSION_WINDOW.

VOD is referer-locked at the origin (/player/vod/ 403s without a tvinus/majotv Referer)
and apps send no Referer, so movies go through /xv/<user>/<exp>/<sig>/<key>/<file>, where
<key> is the movie's folder under the web root (vod/<slug> or vod-us/_majo_new/<slug>) —
an HMAC-signed, expiring path. The gateway serves those files itself (sendfile): the
alfg/nginx-rtmp image has neither auth_request nor secure_link, so nginx can't check it.

Binds 172.17.0.1:8091 (host-only, reached by the nginx-rtmp container), never public.
Members reach it through a Bunny pull zone (tv1.sazatv.com -> origin), which neither
forwards the public hostname nor the client address as the socket peer: links are built
from XTREAM_PUBLIC_HOST and the viewer is the leftmost X-Forwarded-For entry.
Config: env / config/accounts.env — XTREAM_SECRET (required for VOD signing),
XTREAM_PUBLIC_HOST, TVINUS_CHECK_URL, TVINUS_TZ_OFFSET (hours; WordPress stores
Singapore time, UTC+8), XTREAM_PORT.
"""
import hashlib, hmac, json, os, re, sys, threading, time, zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlencode, urljoin
from urllib.error import HTTPError
from urllib.request import Request, urlopen

PROJECT = os.environ.get("PROJECT", "/opt/streaming-stack")
LINEUP = os.path.join(PROJECT, "player", "channels.json")          # published, health-aware
# The PUBLISHED catalogue: nginx mounts ./vod-disk at /var/www/player/vod, so
# player/vod/movies.json on the host is a stale leftover nginx never serves.
MOVIES = os.path.join(PROJECT, "vod-disk", "movies.json")
ENVFILE = os.path.join(PROJECT, "config", "accounts.env")

def _env():
    """os.environ over config/accounts.env (same file the VOD scripts read)."""
    vals = {}
    try:
        for line in open(ENVFILE):
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*?)\s*$", line)
            if m:
                vals[m.group(1)] = m.group(2).strip("'\"")
    except OSError:
        pass
    vals.update(os.environ)
    return vals

ENV = _env()
HOST = ENV.get("XTREAM_BIND", "172.17.0.1")
PORT = int(ENV.get("XTREAM_PORT", "8091"))
CHECK_URL = ENV.get("TVINUS_CHECK_URL",
                    "https://tvinus.com/wp-json/tvinus-reseller/v1/auth/check-vip")
TZ = timezone(timedelta(hours=float(ENV.get("TVINUS_TZ_OFFSET", "8"))))
SECRET = ENV.get("XTREAM_SECRET", "").encode()
PW_FIELD = ENV.get("TVINUS_PW_FIELD", "password_valid")   # must be literally true
PUBLIC_HOST = ENV.get("XTREAM_PUBLIC_HOST", "")    # the DNS members type, e.g. tv1.sazatv.com
# Web-root names in catalogue URLs -> where those disks sit on the host (docker-compose
# mounts ./vod-disk at /var/www/player/vod and ./vod-disk-us at /var/www/player/vod-us).
VOD_ROOTS = {"vod": os.path.join(PROJECT, "vod-disk"),
             "vod-us": os.path.join(PROJECT, "vod-disk-us")}
VOD_TYPES = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t",
             ".vtt": "text/vtt", ".jpg": "image/jpeg", ".png": "image/png"}

AUTH_TTL_OK = 60          # s a good answer is reused (channel flips cost nothing)
AUTH_TTL_BAD = 15         # s a wrong-password/expired answer is reused
AUTH_STALE_MAX = 1800     # s an active user stays on air while tvinus.com is down
# Wrong-password budgets (check-vip is a password-guess oracle). Per (ip,user) stops a
# guesser on one account; the wider per-IP cap stops spraying — sized for carrier NAT, where
# one phone stuck on an old saved password must not lock out its neighbours. A password
# that already succeeded is served from cache and never hits either budget.
# X-Forwarded-For can be forged by anyone hitting the origin directly, so a per-account cap
# that ignores the address backs both up.
FAIL_WINDOW, FAIL_MAX_USER, FAIL_MAX_IP, FAIL_MAX_ACCOUNT = 300, 8, 40, 30
VOD_TOKEN_SECS = 8 * 3600
SESSION_WINDOW = 30       # s without a playlist/segment fetch before a device's slot frees (client: 30s)
VOD_ID_BASE = 90_000_000  # fallback VOD ids, above both channel numbers and movies.json ids

def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)

# ── Catalogue (re-read when the published files change) ─────────────────────────────

class Catalogue:
    def __init__(self):
        self.lock = threading.Lock()
        self.mt = (None, None)
        self.live, self.live_by_id, self.live_cats = [], {}, []
        self.vod, self.vod_by_id, self.vod_cats = [], {}, []

    def _mtime(self, p):
        try:
            return os.stat(p).st_mtime
        except OSError:
            return None

    def refresh(self):
        mt = (self._mtime(LINEUP), self._mtime(MOVIES))
        if mt == self.mt:
            return
        with self.lock:
            if mt == self.mt:
                return
            before = (len(self.live), len(self.vod))
            self._load_live()
            self._load_vod()
            self.mt = mt
            # channels.json is regenerated every ~5s (live_status), so only log real change
            if (len(self.live), len(self.vod)) != before:
                log("catalogue: %d live, %d vod" % (len(self.live), len(self.vod)))

    def _load_live(self):
        try:
            rows = json.load(open(LINEUP))
        except Exception as e:
            log("lineup unreadable: %s" % e)
            return
        cats, live = {}, []
        for c in rows:
            m = re.match(r"channel(\d+)$", c.get("name", ""))
            if not m or not c.get("hls"):
                continue
            country = c.get("country") or "Other"
            cats.setdefault(country, str(len(cats) + 1))
            live.append({"id": int(m.group(1)), "name": c.get("title") or c["name"],
                         "logo": c.get("logo", ""), "hls": c["hls"],
                         "cat": cats[country]})
        live.sort(key=lambda x: x["id"])
        self.live = live
        self.live_by_id = {x["id"]: x for x in live}
        self.live_cats = [{"category_id": v, "category_name": k, "parent_id": 0}
                          for k, v in cats.items()]

    def _load_vod(self):
        try:
            rows = json.load(open(MOVIES))
        except Exception as e:
            log("movies.json unreadable: %s" % e)
            return
        if isinstance(rows, dict):
            rows = rows.get("movies", [])
        # movies.json carries its own stream_id (the same ids the website uses) — keep them,
        # apps store favourites/resume points by stream_id. A row without one (or a
        # duplicate) gets a slug-derived id, so it too survives catalogue regeneration.
        cats, vod = {}, []
        used = {r["stream_id"] for r in rows if isinstance(r.get("stream_id"), int)}
        claimed = set()
        for r in sorted(rows, key=lambda r: r.get("slug", "")):
            m = re.match(r".*/player/((?:vod|vod-us)(?:/[A-Za-z0-9._-]+)+)/index\.m3u8$",
                         r.get("m3u8_url", ""))
            if not m or "/../" in m.group(1) + "/":
                continue
            key = m.group(1)
            slug = key.rsplit("/", 1)[1]
            sid = r.get("stream_id")
            if not isinstance(sid, int) or sid in claimed:
                sid = VOD_ID_BASE + zlib.crc32(slug.encode()) % 9_000_000
                while sid in used:
                    sid += 1
                used.add(sid)
            claimed.add(sid)
            genre = (r.get("genre") or "Movies").split(",")[0].strip() or "Movies"
            cats.setdefault(genre, str(len(cats) + 1))
            vod.append({"id": sid, "slug": slug, "key": key, "name": r.get("title") or slug,
                        "year": str(r.get("year") or ""), "rating": r.get("rating") or 0,
                        "logo": r.get("logo_url", ""), "plot": r.get("plot", ""),
                        "cast": r.get("cast", ""), "director": r.get("director", ""),
                        "genre": r.get("genre", ""), "duration": r.get("duration", ""),
                        "added": str(r.get("added") or 0), "cat": cats[genre]})
        self.vod = vod
        self.vod_by_id = {x["id"]: x for x in vod}
        self.vod_cats = [{"category_id": v, "category_name": k, "parent_id": 0}
                         for k, v in cats.items()]

CAT = Catalogue()

# ── Auth against the TViNUS panel ───────────────────────────────────────────────────

_auth_cache = {}      # (user, sha256(pw)) -> (ts, account-or-None)
_fails = {}           # ip or (ip,user) -> [timestamps of wrong-password attempts]
_auth_lock = threading.Lock()

def _check_vip(user, pw):
    """Raw check-vip call. Returns the JSON dict, or raises on transport failure."""
    body = urlencode({"username": user, "password": pw}).encode()
    req = Request(CHECK_URL, data=body, headers={"User-Agent": "kozee-xtream/1.0",
                  "Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=8) as r:
        return json.load(r)

def _parse_exp(s):
    try:
        return int(datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
    except Exception:
        return None

def _unreachable(user, err, hit, now):
    """tvinus.com down/erroring: keep a previously-active viewer on air, fail closed
    otherwise. A 401 is NOT this — it's a wrong password and must count as one."""
    log("check-vip failed for %s: %s" % (user, str(err)[:120]))
    if hit and hit[1] and hit[1]["active"] and now - hit[0] < AUTH_STALE_MAX:
        return hit[1]
    return None

def authenticate(user, pw, ip):
    """-> account dict {user, active, exp, max_conn} or None for bad credentials.
    Raises PermissionError when this IP has burnt its wrong-password budget."""
    if not user or not pw:
        return None
    now = time.time()
    key = (user, hashlib.sha256(pw.encode()).hexdigest())
    with _auth_lock:
        hit = _auth_cache.get(key)
        if hit and hit[1] and now - hit[0] < AUTH_TTL_OK:
            return hit[1]                     # proven password: budgets don't apply
        for k, cap in ((ip, FAIL_MAX_IP), ((ip, user), FAIL_MAX_USER),
                       (("*", user), FAIL_MAX_ACCOUNT)):
            recent = [t for t in _fails.get(k, []) if now - t < FAIL_WINDOW]
            _fails[k] = recent
            if len(recent) >= cap:
                raise PermissionError(ip)
    if hit and hit[1] is None and now - hit[0] < AUTH_TTL_BAD:
        return None
    try:
        d = _check_vip(user, pw)
    except HTTPError as e:
        if e.code not in (400, 401, 403):
            return _unreachable(user, e, hit, now)
        d = {}                    # definitive "wrong username/password" -> counted below
    except Exception as e:
        return _unreachable(user, e, hit, now)
    if not d.get("user_id") or d.get(PW_FIELD) is not True:
        if d.get("user_id") and PW_FIELD not in d:
            log("check-vip sent no %r for %s: password unverified, refusing" % (PW_FIELD, user))
        acct = None
        with _auth_lock:
            _fails.setdefault(ip, []).append(now)
            _fails.setdefault((ip, user), []).append(now)
            _fails.setdefault(("*", user), []).append(now)
    else:
        exp = _parse_exp(d.get("expires_at") or "")
        if exp is None and float(d.get("points") or 0) > 0:
            # Points burn on the calendar at a fixed 24/day (1 point = 1 hour, per the
            # client), so the balance itself is the time left when expires_at is absent.
            exp = int(now + float(d["points"]) * 3600)
        active = bool(d.get("is_vip") or d.get("authorized")) and (exp is None or exp > now)
        try:
            max_conn = max(1, int(d.get("max_connections") or 1))   # per user level
        except (TypeError, ValueError):
            max_conn = 1
        with _sess_lock:
            _limits[user] = max_conn
        acct = {"user": user, "active": active, "exp": exp, "max_conn": max_conn,
                "points": d.get("points", 0)}
    with _auth_lock:
        _auth_cache[key] = (now, acct)
    return acct

# ── Device (connection) limits ──────────────────────────────────────────────────

_sessions = {}        # user -> {device: last_seen}
_limits = {}          # user -> max_connections from the last successful check-vip
_sess_lock = threading.Lock()

def device_key(ip, ua):
    return "%s|%s" % (ip, hashlib.sha1((ua or "").encode()).hexdigest()[:10])

def claim_slot(user, device):
    """-> "new" (device just started), "same" (already watching) or "full" (refused)."""
    now = time.time()
    with _sess_lock:
        devs = {d: t for d, t in _sessions.get(user, {}).items() if now - t < SESSION_WINDOW}
        state = "same" if device in devs else "new"
        if state == "new" and len(devs) >= _limits.get(user, 1):
            _sessions[user] = devs
            return "full"
        devs[device] = now
        _sessions[user] = devs
        return state

def active_count(user):
    now = time.time()
    with _sess_lock:
        return sum(1 for t in _sessions.get(user, {}).values() if now - t < SESSION_WINDOW)

# ── Signed VOD paths ────────────────────────────────────────────────────────────────

def vod_sig(user, exp, key):
    msg = ("%s|%d|%s" % (user, exp, key)).encode()
    return hmac.new(SECRET, msg, hashlib.sha256).hexdigest()[:24]

def vod_path(user, key):
    exp = int(time.time()) + VOD_TOKEN_SECS
    return "/xv/%s/%d/%s/%s/index.m3u8" % (quote(user, safe=""), exp, vod_sig(user, exp, key), key)

def vod_path_ok(uri):
    """/xv/<user>/<exp>/<sig>/<key>/<file...> -> signed for some folder prefix, unexpired.
    The key's depth varies (vod/<slug> vs vod-us/_majo_new/<slug>) and a playlist may name
    files in subfolders (subtitles), so any directory prefix whose signature matches wins."""
    from urllib.parse import unquote
    m = re.match(r"^/xv/([^/]+)/(\d+)/([0-9a-f]{24})/((?:vod|vod-us)/[A-Za-z0-9._/-]+)$",
                 urlparse(uri).path)
    if not m or not SECRET or int(m.group(2)) <= time.time():
        return False
    user, exp, sig, rest = unquote(m.group(1)), int(m.group(2)), m.group(3), m.group(4)
    parts = rest.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return any(hmac.compare_digest(sig, vod_sig(user, exp, "/".join(parts[:i])))
               for i in range(2, len(parts)))

# ── Xtream payloads ─────────────────────────────────────────────────────────────────

def user_info(acct, user, pw):
    return {"username": user, "password": pw,
            "message": "" if acct["active"] else "Subscription expired",
            "auth": 1, "status": "Active" if acct["active"] else "Expired",
            "exp_date": str(acct["exp"]) if acct["exp"] else None,
            "is_trial": "0", "active_cons": str(active_count(user)), "created_at": None,
            "max_connections": str(acct["max_conn"]),
            # HLS only: apps that honour this pick .m3u8; .ts requests are redirected
            "allowed_output_formats": ["m3u8"]}

def server_info(host, https):
    now = datetime.now(timezone.utc)
    name, _, port = host.partition(":")
    return {"url": name, "port": port or "80", "https_port": "443",
            "server_protocol": "https" if https else "http", "rtmp_port": "1935",
            "timezone": "UTC", "timestamp_now": int(now.timestamp()),
            "time_now": now.strftime("%Y-%m-%d %H:%M:%S"), "process": True}

def live_rows(cat_id=None):
    return [{"num": i + 1, "name": c["name"], "stream_type": "live", "stream_id": c["id"],
             "stream_icon": c["logo"], "epg_channel_id": None, "added": "0",
             "is_adult": 0, "category_id": c["cat"], "category_ids": [int(c["cat"])],
             "custom_sid": "", "tv_archive": 0, "direct_source": "", "tv_archive_duration": 0}
            for i, c in enumerate(CAT.live) if cat_id in (None, "", c["cat"])]

def vod_rows(cat_id=None):
    return [{"num": i + 1, "name": v["name"], "title": v["name"], "year": v["year"],
             "stream_type": "movie", "stream_id": v["id"], "stream_icon": v["logo"],
             "rating": str(v["rating"]), "rating_5based": round(float(v["rating"] or 0) / 2, 1),
             "added": v["added"], "is_adult": 0, "category_id": v["cat"],
             "category_ids": [int(v["cat"])], "container_extension": "m3u8",
             "custom_sid": "", "direct_source": ""}
            for i, v in enumerate(CAT.vod) if cat_id in (None, "", v["cat"])]

def vod_info(sid):
    v = CAT.vod_by_id.get(sid)
    if not v:
        return {"info": [], "movie_data": []}
    secs = 0
    m = re.match(r"(\d+):(\d+):(\d+)", v["duration"] or "")
    if m:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return {"info": {"name": v["name"], "movie_image": v["logo"], "cover_big": v["logo"],
                     "plot": v["plot"], "description": v["plot"], "cast": v["cast"],
                     "actors": v["cast"], "director": v["director"], "genre": v["genre"],
                     "releasedate": v["year"], "duration": v["duration"],
                     "duration_secs": secs, "rating": str(v["rating"]), "tmdb_id": "",
                     "backdrop_path": [], "youtube_trailer": ""},
            "movie_data": {"stream_id": v["id"], "name": v["name"], "added": v["added"],
                           "category_id": v["cat"], "container_extension": "m3u8",
                           "custom_sid": "", "direct_source": ""}}

def m3u(base, user, pw, with_vod):
    u, p = quote(user, safe=""), quote(pw, safe="")
    out = ["#EXTM3U"]
    cats = {c["category_id"]: c["category_name"] for c in CAT.live_cats}
    for c in CAT.live:
        out.append('#EXTINF:-1 tvg-id="" tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
                   % (c["name"], c["logo"], cats.get(c["cat"], ""), c["name"]))
        out.append("%s/live/%s/%s/%d.m3u8" % (base, u, p, c["id"]))
    if with_vod:
        vcats = {c["category_id"]: c["category_name"] for c in CAT.vod_cats}
        for v in CAT.vod:
            out.append('#EXTINF:-1 tvg-id="" tvg-name="%s" tvg-logo="%s" group-title="VOD - %s",%s'
                       % (v["name"], v["logo"], vcats.get(v["cat"], ""), v["name"]))
            out.append("%s/movie/%s/%s/%d.m3u8" % (base, u, p, v["id"]))
    return "\n".join(out) + "\n"

# ── HTTP ────────────────────────────────────────────────────────────────────────────

class H(BaseHTTPRequestHandler):
    server_version = "nginx"          # don't advertise what's behind the DNS
    sys_version = ""

    def log_message(self, fmt, *a):
        pass                          # request lines carry passwords — log events, not URLs

    def ip(self):
        xff = self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        return xff or self.headers.get("X-Real-IP") or self.client_address[0]

    def send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("application/json", "text/", "audio/")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def redirect(self, loc):
        self.send(302, b"", "text/plain", {"Location": loc})

    def base(self):
        """Public base URL for links we hand out. Behind Bunny the Host reaching us is the
        origin's, so the configured public host wins; the scheme follows what the viewer
        used (nginx forwards the CDN's X-Forwarded-Proto, else its own $scheme)."""
        https = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"
        host = PUBLIC_HOST or self.headers.get("Host", "localhost")
        return ("https://" if https else "http://") + host, host, https

    def acct(self, user, pw):
        try:
            return authenticate(user, pw, self.ip())
        except PermissionError:
            self.send(429, {"user_info": {"auth": 0, "message": "Too many attempts"}})
            return False

    def do_OPTIONS(self):             # CORS preflight from browser-based players
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):                # some apps POST player_api.php with a form body
        n = int(self.headers.get("Content-Length") or 0)
        self._form = parse_qs(self.rfile.read(min(n, 65536)).decode("utf-8", "replace"))
        self.do_GET()

    def do_GET(self):
        CAT.refresh()
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        q.update({k: v[0] for k, v in getattr(self, "_form", {}).items()})
        path = url.path
        try:
            if path.startswith("/xv/"):
                return self.vod_file(path)
            if path == "/player_api.php":
                return self.player_api(q)
            if path == "/get.php":
                return self.get_php(q)
            if path == "/xmltv.php":
                return self.send(200, '<?xml version="1.0" encoding="UTF-8"?>\n<tv></tv>\n',
                                 "application/xml")
            m = re.match(r"^/(?:(live|movie)/)?([^/]+)/([^/]+)/(\d+)(?:\.(\w+))?$", path)
            if m:
                return self.play(m.group(1) or "live", m.group(2), m.group(3), int(m.group(4)),
                                 (m.group(5) or "").lower())
            return self.send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            log("error on %s: %s" % (path, e))
            self.send(500, {"error": "server error"})

    def player_api(self, q):
        user, pw = q.get("username", ""), q.get("password", "")
        acct = self.acct(user, pw)
        if acct is False:
            return
        if not acct:
            # Never log the password. Its length exposes the classic failure: a password
            # with # ? & / % or a space is cut short in the app's URL before it reaches us.
            log("login refused %s ip=%s pwlen=%d" % (user, self.ip(), len(pw)))
            return self.send(200, {"user_info": {"auth": 0}})
        action = q.get("action", "")
        if not action:
            _, host, https = self.base()
            return self.send(200, {"user_info": user_info(acct, user, pw),
                                   "server_info": server_info(host, https)})
        if not acct["active"]:
            return self.send(200, [])
        cat = q.get("category_id")
        if action == "get_live_categories":
            return self.send(200, CAT.live_cats)
        if action == "get_live_streams":
            return self.send(200, live_rows(cat))
        if action == "get_vod_categories":
            return self.send(200, CAT.vod_cats)
        if action == "get_vod_streams":
            return self.send(200, vod_rows(cat))
        if action == "get_vod_info":
            return self.send(200, vod_info(int(q.get("vod_id") or 0)))
        if action in ("get_series_categories", "get_series"):
            return self.send(200, [])
        if action == "get_series_info":
            return self.send(200, {"seasons": [], "info": {}, "episodes": {}})
        if action in ("get_short_epg", "get_simple_data_table"):
            return self.send(200, {"epg_listings": []})
        return self.send(200, [])

    def get_php(self, q):
        user, pw = q.get("username", ""), q.get("password", "")
        acct = self.acct(user, pw)
        if acct is False:
            return
        if not acct or not acct["active"]:
            return self.send(403, "Forbidden\n", "text/plain")
        base, _, _ = self.base()
        body = m3u(base, user, pw, q.get("type", "m3u_plus") == "m3u_plus")
        self.send(200, body, "audio/x-mpegurl",
                  {"Content-Disposition": 'attachment; filename="playlist.m3u"'})

    def slot(self, user, what):
        """Device-limit gate for a playlist/segment fetch; False = refused (already sent)."""
        state = claim_slot(user, device_key(self.ip(), self.headers.get("User-Agent")))
        if state == "full":
            log("REFUSED %s %s ip=%s: all %d device slot(s) in use"
                % (user, what, self.ip(), _limits.get(user, 1)))
            self.send(403, "Maximum connections reached\n", "text/plain")
            return False
        if state == "new":
            log("device start %s %s ip=%s (%d/%d)"
                % (user, what, self.ip(), active_count(user), _limits.get(user, 1)))
        return True

    def play(self, kind, user, pw, sid, ext):
        from urllib.parse import unquote
        user, pw = unquote(user), unquote(pw)
        acct = self.acct(user, pw)
        if acct is False:
            return
        if not acct or not acct["active"]:
            return self.send(403, "Forbidden\n", "text/plain")
        base, _, _ = self.base()
        if kind == "live":
            c = CAT.live_by_id.get(sid)
            if not c:
                return self.send(404, "Not found\n", "text/plain")
            if ext == "ts":           # we only produce HLS; send MPEG-TS-mode apps to it
                return self.redirect("%s/live/%s/%s/%d.m3u8"
                                     % (base, quote(user, safe=""), quote(pw, safe=""), sid))
            if not self.slot(user, "ch%d" % sid):
                return
            return self.live_playlist(c)
        v = CAT.vod_by_id.get(sid)
        if not v or not SECRET:
            return self.send(404, "Not found\n", "text/plain")
        log("play vod %s %s ip=%s" % (user, v["slug"], self.ip()))
        return self.redirect(base + vod_path(user, v["key"]))

    def live_playlist(self, c):
        """The channel's live playlist from local disk, segment URIs made absolute to the
        CDN — the app polls US (device accounting) but pulls video from the CDN."""
        hls_root = os.path.realpath(os.path.join(PROJECT, "hls"))
        fp = os.path.realpath(os.path.join(PROJECT, urlparse(c["hls"]).path.lstrip("/")))
        cdn_dir = c["hls"].rsplit("/", 1)[0] + "/"
        try:
            if not fp.startswith(hls_root + os.sep):
                raise OSError(fp)
            text = open(fp, encoding="utf-8", errors="replace").read()
        except OSError:
            return self.redirect(c["hls"])          # no local copy: hand over to the CDN
        out = []
        for line in text.splitlines():
            if line and not line.startswith("#"):
                line = urljoin(cdn_dir, line)
            elif 'URI="' in line:
                line = re.sub(r'URI="([^"]+)"',
                              lambda m: 'URI="%s"' % urljoin(cdn_dir, m.group(1)), line)
            out.append(line)
        self.send(200, "\n".join(out) + "\n", "application/vnd.apple.mpegurl")


    def vod_file(self, path):
        if not vod_path_ok(path):
            return self.send(403, "Forbidden\n", "text/plain")
        from urllib.parse import unquote
        if path.endswith((".m3u8", ".ts")) and not self.slot(unquote(path.split("/")[2]), "vod"):
            return
        rest = path.split("/", 5)[5]                 # <vod|vod-us>/<folder...>/<file>
        root, _, rel = rest.partition("/")
        base = os.path.realpath(VOD_ROOTS[root])
        fp = os.path.realpath(os.path.join(base, rel))
        ctype = VOD_TYPES.get(os.path.splitext(fp)[1].lower())
        if not fp.startswith(base + os.sep) or not ctype or not os.path.isfile(fp):
            return self.send(404, "Not found\n", "text/plain")
        with open(fp, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.connection.sendfile(f)


def main():
    if not SECRET:
        log("WARNING: XTREAM_SECRET unset — VOD playback disabled")
    CAT.refresh()
    srv = ThreadingHTTPServer((HOST, PORT), H)
    srv.daemon_threads = True
    log("xtream gateway on %s:%d (auth: %s)" % (HOST, PORT, CHECK_URL))
    srv.serve_forever()


if __name__ == "__main__":
    main()
