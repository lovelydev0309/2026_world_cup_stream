#!/usr/bin/env python3
"""Xtream-Codes-compatible gateway — the "DNS" external IPTV apps log in to.

TViNUS reseller members (WordPress users on tvinus.com) enter this server's address plus
their panel username/password in TiviMate / IPTV Smarters / XCIPTV and get our live
lineup + VOD, exactly like a line from a code panel. Python stdlib only (like admin_api).

Routes (nginx server block for the DNS host proxies everything here):
  GET /player_api.php?username&password[&action=...]   user_info/server_info + catalogue
  GET /get.php?username&password[&type=m3u_plus]        M3U playlist
  GET /xmltv.php                                        empty guide (apps want a 200)
  GET /live/<u>/<p>/<id>.m3u8                           live playlist, segments on the CDN
  GET /live/<u>/<p>/<id>.ts                             continuous MPEG-TS (apps in TS mode)
  GET /movie/<u>/<p>/<id>.m3u8                          VOD playlist, absolute signed /xv/ segments
  GET /movie/<u>/<p>/<id>.<other ext>                   whole movie as one MPEG-TS file (Range)
  GET /movie/img/<vod|vod-us>/<folder>/<image>          poster (no Referer needed, unlike the CDN)
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
so each poll proves a device is still watching. A device is the viewer's IP; a NEW device
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
# The PUBLISHED movie catalogues and their language: nginx mounts ./vod-disk at
# /player/vod (MajoTV, Spanish) and ./vod-disk-us at /player/vod-us (KozeeTV, English);
# player/vod/movies.json on the host is a stale leftover nginx never serves.
CATALOGUES = [(os.path.join(PROJECT, "vod-disk", "movies.json"), "es"),
              (os.path.join(PROJECT, "vod-disk-us", "movies.json"), "en")]
EN_ID_OFFSET = 200_000_000   # both catalogues reuse provider stream_ids (14 collide)

# Channel packages, chosen per member by the agent in the panel and returned by
# check-vip as `bouquet`: which live countries (channels.json "country") and movie
# languages the member gets. Missing/unknown = everything, so members created before
# packages existed keep the full lineup.
PACKAGES = {"es": ({"Mexico"}, {"es"}),           # Spanish: Mexican channels + Spanish movies
            "us": ({"US"}, {"en"})}               # USA: US channels + English movies
PACKAGE_ALIASES = {"spanish": "es", "espanol": "es", "español": "es", "mexico": "es",
                   "mx": "es", "latino": "es", "english": "us", "usa": "us", "en": "us",
                   "america": "us", "united states": "us"}

def package_of(d):
    v = str(d.get("bouquet") or "").strip().lower()
    v = PACKAGE_ALIASES.get(v, v)
    return v if v in PACKAGES else "all"

def live_ok(acct, c):
    pk = PACKAGES.get(acct.get("package", "all"))
    return pk is None or c["country"] in pk[0]

def vod_ok(acct, v):
    pk = PACKAGES.get(acct.get("package", "all"))
    return pk is None or v["lang"] in pk[1]
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
# check-vip takes 2-7s, so a channel switch that needed a fresh check felt like a dead
# click. An ACTIVE answer up to this old is served at once and re-checked in the
# background (stale-while-revalidate); running .ts streams still re-auth every 60s.
AUTH_SWR_MAX = 600
# Wrong-password budgets (check-vip is a password-guess oracle). Per (ip,user) stops a
# guesser on one account; the wider per-IP cap stops spraying — sized for carrier NAT, where
# one phone stuck on an old saved password must not lock out its neighbours. A password
# that already succeeded is served from cache and never hits either budget.
# X-Forwarded-For can be forged by anyone hitting the origin directly, so a per-account cap
# that ignores the address backs both up.
FAIL_WINDOW, FAIL_MAX_USER, FAIL_MAX_IP, FAIL_MAX_ACCOUNT = 300, 8, 40, 30
VOD_TOKEN_SECS = 8 * 3600
# MPEG-TS output: Smarters/TiviMate default to /live/…/<id>.ts and expect ONE endless TS
# stream (a redirect to an HLS playlist makes them fail with "Playback error"), so those
# requests get the channel's local HLS segments sent back to back as they appear.
TS_START_SEGS = 3         # start this many segments behind the live edge (startup cushion)
TS_IDLE_MAX = 45          # s with no new segment before giving up (channel stalled)
TS_REAUTH_SECS = 60       # re-check the account while streaming (expiry/password change)
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
        mt = (self._mtime(LINEUP),) + tuple(self._mtime(f) for f, _ in CATALOGUES)
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
                         "country": country, "cat": cats[country]})
        live.sort(key=lambda x: x["id"])
        self.live = live
        self.live_by_id = {x["id"]: x for x in live}
        self.live_cats = [{"category_id": v, "category_name": k, "parent_id": 0}
                          for k, v in cats.items()]

    def _load_vod(self):
        books = []
        for f, lang in CATALOGUES:
            try:
                rows = json.load(open(f))
            except Exception as e:
                log("%s unreadable: %s" % (f, e))
                return                  # keep the previous catalogue rather than a half one
            books.append((lang, rows.get("movies", []) if isinstance(rows, dict) else rows))
        # movies.json carries its own stream_id (the Spanish ones are the ids the website
        # uses) — keep them, apps store favourites/resume points by stream_id. English ids
        # are offset (the catalogues share provider ids). A row without one, or a
        # duplicate, gets a slug-derived id, so it too survives catalogue regeneration.
        cats, vod = {}, []
        used = {r["stream_id"] + (EN_ID_OFFSET if lang == "en" else 0)
                for lang, rows in books for r in rows if isinstance(r.get("stream_id"), int)}
        claimed = set()
        for lang, r in [(lang, r) for lang, rows in books
                        for r in sorted(rows, key=lambda r: r.get("slug", ""))]:
            if r.get("hidden"):
                continue        # removed/blocked titles: the website hides them, so do we
            m = re.match(r".*/player/((?:vod|vod-us)(?:/[A-Za-z0-9._-]+)+)/index\.m3u8$",
                         r.get("m3u8_url", ""))
            if not m or "/../" in m.group(1) + "/":
                continue
            key = m.group(1)
            slug = key.rsplit("/", 1)[1]
            sid = r.get("stream_id")
            if isinstance(sid, int) and lang == "en":
                sid += EN_ID_OFFSET
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
                        "added": str(r.get("added") or 0), "lang": lang,
                        "cat": cats[genre]})
        self.vod = vod
        self.vod_by_id = {x["id"]: x for x in vod}
        self.vod_cats = [{"category_id": v, "category_name": k, "parent_id": 0}
                         for k, v in cats.items()]

CAT = Catalogue()

# ── Auth against the TViNUS panel ───────────────────────────────────────────────────

_auth_cache = {}      # (user, sha256(pw)) -> (ts, account-or-None)
_fails = {}           # ip or (ip,user) -> [timestamps of wrong-password attempts]
_auth_lock = threading.Lock()
_revalidating = set()  # cache keys with a background check-vip in flight

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
        acct = hit[1] if hit else None
        if (acct and acct["active"] and now - hit[0] < AUTH_SWR_MAX
                and not (acct["exp"] and acct["exp"] <= now)):
            if key not in _revalidating:      # answer now, refresh behind the scenes
                _revalidating.add(key)
                threading.Thread(target=_revalidate, args=(user, pw, ip, key),
                                 daemon=True).start()
            return acct
        for k, cap in ((ip, FAIL_MAX_IP), ((ip, user), FAIL_MAX_USER),
                       (("*", user), FAIL_MAX_ACCOUNT)):
            recent = [t for t in _fails.get(k, []) if now - t < FAIL_WINDOW]
            _fails[k] = recent
            if len(recent) >= cap:
                raise PermissionError(ip)
    if hit and hit[1] is None and now - hit[0] < AUTH_TTL_BAD:
        return None
    return _remote_check(user, pw, ip, key, hit, now)

def _revalidate(user, pw, ip, key):
    try:
        with _auth_lock:
            hit = _auth_cache.get(key)
        _remote_check(user, pw, ip, key, hit, time.time())
    finally:
        with _auth_lock:
            _revalidating.discard(key)

def _remote_check(user, pw, ip, key, hit, now):
    """Ask check-vip, cache and return the answer (shared by the inline and background
    paths)."""
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
                "package": package_of(d),
                "points": d.get("points", 0)}
    with _auth_lock:
        _auth_cache[key] = (now, acct)
    return acct

# ── Device (connection) limits ──────────────────────────────────────────────────

_sessions = {}        # user -> {device: last_seen}
_limits = {}          # user -> max_connections from the last successful check-vip
_sess_lock = threading.Lock()

def device_key(ip, ua):
    """One viewer location = one device. NOT ip+User-Agent: Smarters for Windows opens a
    movie with an NSPlayer/7.10 probe and then plays it with VLC/2.2.4, so a UA-keyed
    limit counted one movie as two devices and refused the real player on a 1-device
    plan. (A household's TVs share an IP and count as one — the lenient side.)"""
    return ip

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

def local_playlist(c):
    """Path of a channel's live playlist on this host's disk, or None if it isn't there."""
    hls_root = os.path.realpath(os.path.join(PROJECT, "hls"))
    fp = os.path.realpath(os.path.join(PROJECT, urlparse(c["hls"]).path.lstrip("/")))
    return fp if fp.startswith(hls_root + os.sep) and os.path.isfile(fp) else None

def playlist_segments(fp):
    """[(media sequence, segment path)] of a local media playlist."""
    seq, out = 0, []
    with open(fp, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                seq = int(line.split(":", 1)[1] or 0)
            elif line and not line.startswith("#"):
                out.append((seq + len(out), os.path.join(os.path.dirname(fp), line)))
    return out

class TsRebase:
    """Keeps an endless MPEG-TS stream's clock continuous across encoder restarts.

    Our channels restart often (64 of 95 had a restart in their 160s window, 2026-10-17)
    and each restart leaps the timestamps (e.g. 240s -> 95443s). HLS players get an
    EXT-X-DISCONTINUITY tag for that; a raw .ts stream has no such signal, so players
    freeze or reconnect. When any PTS or PCR jumps by more than JUMP, one shared offset
    is re-based so the stream continues ~1 frame after where it was — audio and video
    move together, so they stay in sync. Applied to PES PTS/DTS and adaptation-field PCR.
    """
    WRAP = 1 << 33                       # 33-bit 90 kHz clock
    JUMP = 2 * 90000                     # >2s either way = a restart, not B-frame reorder

    def __init__(self):
        self.offset = 0
        self.last = {}                   # ("pts"|"pcr", pid) -> last ADJUSTED value
        self.rebases = 0

    def _shift(self, key, ts, detect=True):
        a = (ts + self.offset) % self.WRAP
        prev = self.last.get(key)
        if detect and prev is not None:
            d = (a - prev + self.WRAP // 2) % self.WRAP - self.WRAP // 2
            if d > self.JUMP or d < -self.JUMP:
                self.offset = (self.offset - d + 3000) % self.WRAP
                a = (ts + self.offset) % self.WRAP
                self.rebases += 1
        if detect:
            self.last[key] = a
        return a

    @staticmethod
    def _get_ts(b, i):
        return (((b[i] >> 1) & 7) << 30 | b[i + 1] << 22 | (b[i + 2] >> 1) << 15
                | b[i + 3] << 7 | b[i + 4] >> 1)

    @staticmethod
    def _put_ts(b, i, v):
        b[i] = (b[i] & 0xF0) | ((v >> 30) & 7) << 1 | 1
        b[i + 1] = (v >> 22) & 0xFF
        b[i + 2] = ((v >> 15) & 0x7F) << 1 | 1
        b[i + 3] = (v >> 7) & 0xFF
        b[i + 4] = (v & 0x7F) << 1 | 1

    def process(self, data):
        b = bytearray(data)
        for i in range(0, len(b) - 187, 188):
            if b[i] != 0x47:
                continue
            pid = (b[i + 1] & 0x1F) << 8 | b[i + 2]
            afc = (b[i + 3] >> 4) & 3
            p = i + 4
            pcr_at = None
            if afc & 2:
                af_len = b[i + 4]
                if af_len >= 7 and b[i + 5] & 0x10:
                    pcr_at = i + 6
                p = i + 5 + af_len
            # PES first: in a packet carrying both, the restart is detected on the PTS and
            # the PCR then gets the same new offset.
            if (b[i + 1] & 0x40 and afc & 1 and p + 14 <= i + 188
                    and b[p] == 0 and b[p + 1] == 0 and b[p + 2] == 1):
                flags = b[p + 7] >> 6
                if flags & 2:
                    self._put_ts(b, p + 9, self._shift(("pts", pid), self._get_ts(b, p + 9)))
                    if flags == 3 and p + 19 <= i + 188:
                        self._put_ts(b, p + 14,
                                     self._shift(("dts", pid), self._get_ts(b, p + 14), False))
            if pcr_at is not None:
                j = pcr_at
                base = b[j] << 25 | b[j + 1] << 17 | b[j + 2] << 9 | b[j + 3] << 1 | b[j + 4] >> 7
                nb = self._shift(("pcr", pid), base)
                b[j], b[j + 1], b[j + 2], b[j + 3] = (nb >> 25) & 0xFF, (nb >> 17) & 0xFF, (nb >> 9) & 0xFF, (nb >> 1) & 0xFF
                b[j + 4] = (b[j + 4] & 0x7F) | (nb & 1) << 7
        return bytes(b)

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
            "allowed_output_formats": ["m3u8", "ts"]}

def server_info(host, https):
    now = datetime.now(timezone.utc)
    name, _, port = host.partition(":")
    return {"url": name, "port": port or "80", "https_port": "443",
            "server_protocol": "https" if https else "http", "rtmp_port": "1935",
            "timezone": "UTC", "timestamp_now": int(now.timestamp()),
            "time_now": now.strftime("%Y-%m-%d %H:%M:%S"), "process": True}

def live_cats(acct):
    have = {c["cat"] for c in CAT.live if live_ok(acct, c)}
    return [x for x in CAT.live_cats if x["category_id"] in have]

def vod_cats(acct):
    have = {v["cat"] for v in CAT.vod if vod_ok(acct, v)}
    return [x for x in CAT.vod_cats if x["category_id"] in have]

def live_rows(acct, cat_id=None):
    return [{"num": i + 1, "name": c["name"], "stream_type": "live", "stream_id": c["id"],
             "stream_icon": c["logo"], "epg_channel_id": None, "added": "0",
             "is_adult": 0, "category_id": c["cat"], "category_ids": [int(c["cat"])],
             "custom_sid": "", "tv_archive": 0, "direct_source": "", "tv_archive_duration": 0}
            for i, c in enumerate([c for c in CAT.live if live_ok(acct, c)])
            if cat_id in (None, "", c["cat"])]

def poster_url(base, logo):
    """Posters live under the referer-locked /player/vod/ on the CDN, which 403s every app;
    hand apps our own /movie/img/ copy instead (same file, read from disk)."""
    m = re.match(r"^https?://[^/]+/player/((?:vod|vod-us)/.+)$", logo or "")
    return "%s/movie/img/%s" % (base, m.group(1)) if m and base else logo

def vod_rows(acct, base, cat_id=None):
    return [{"num": i + 1, "name": v["name"], "title": v["name"], "year": v["year"],
             "stream_type": "movie", "stream_id": v["id"],
             "stream_icon": poster_url(base, v["logo"]),
             "rating": str(v["rating"]), "rating_5based": round(float(v["rating"] or 0) / 2, 1),
             "added": v["added"], "is_adult": 0, "category_id": v["cat"],
             "category_ids": [int(v["cat"])], "container_extension": "m3u8",
             "custom_sid": "", "direct_source": ""}
            for i, v in enumerate([v for v in CAT.vod if vod_ok(acct, v)])
            if cat_id in (None, "", v["cat"])]

def vod_info(acct, base, sid):
    v = CAT.vod_by_id.get(sid)
    if not v or not vod_ok(acct, v):
        return {"info": [], "movie_data": []}
    secs = 0
    m = re.match(r"(\d+):(\d+):(\d+)", v["duration"] or "")
    if m:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    img = poster_url(base, v["logo"])
    return {"info": {"name": v["name"], "movie_image": img, "cover_big": img,
                     "plot": v["plot"], "description": v["plot"], "cast": v["cast"],
                     "actors": v["cast"], "director": v["director"], "genre": v["genre"],
                     "releasedate": v["year"], "duration": v["duration"],
                     "duration_secs": secs, "rating": str(v["rating"]), "tmdb_id": "",
                     "backdrop_path": [], "youtube_trailer": ""},
            "movie_data": {"stream_id": v["id"], "name": v["name"], "added": v["added"],
                           "category_id": v["cat"], "container_extension": "m3u8",
                           "custom_sid": "", "direct_source": ""}}

def m3u(acct, base, user, pw, with_vod):
    u, p = quote(user, safe=""), quote(pw, safe="")
    out = ["#EXTM3U"]
    cats = {c["category_id"]: c["category_name"] for c in CAT.live_cats}
    for c in CAT.live:
        if not live_ok(acct, c):
            continue
        out.append('#EXTINF:-1 tvg-id="" tvg-name="%s" tvg-logo="%s" group-title="%s",%s'
                   % (c["name"], c["logo"], cats.get(c["cat"], ""), c["name"]))
        out.append("%s/live/%s/%s/%d.m3u8" % (base, u, p, c["id"]))
    if with_vod:
        vcats = {c["category_id"]: c["category_name"] for c in CAT.vod_cats}
        for v in CAT.vod:
            if not vod_ok(acct, v):
                continue
            out.append('#EXTINF:-1 tvg-id="" tvg-name="%s" tvg-logo="%s" group-title="VOD - %s",%s'
                       % (v["name"], poster_url(base, v["logo"]), vcats.get(v["cat"], ""),
                          v["name"]))
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
            if path.startswith("/movie/img/"):
                return self.vod_image(path[len("/movie/img/"):])
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
            return self.send(200, live_cats(acct))
        if action == "get_live_streams":
            return self.send(200, live_rows(acct, cat))
        if action == "get_vod_categories":
            return self.send(200, vod_cats(acct))
        if action == "get_vod_streams":
            return self.send(200, vod_rows(acct, self.base()[0], cat))
        if action == "get_vod_info":
            return self.send(200, vod_info(acct, self.base()[0], int(q.get("vod_id") or 0)))
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
        body = m3u(acct, base, user, pw, q.get("type", "m3u_plus") == "m3u_plus")
        self.send(200, body, "audio/x-mpegurl",
                  {"Content-Disposition": 'attachment; filename="playlist.m3u"'})

    def slot(self, user, what):
        """Device-limit gate for a playlist/segment fetch; False = refused (already sent)."""
        ua = self.headers.get("User-Agent") or ""
        state = claim_slot(user, device_key(self.ip(), ua))
        if state == "full":
            log("REFUSED %s %s ip=%s ua=%.40s: all %d device slot(s) in use"
                % (user, what, self.ip(), ua, _limits.get(user, 1)))
            self.send(403, "Maximum connections reached\n", "text/plain")
            return False
        if state == "new":
            log("device start %s %s ip=%s ua=%.40s (%d/%d)"
                % (user, what, self.ip(), ua, active_count(user), _limits.get(user, 1)))
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
            if not live_ok(acct, c):
                return self.send(403, "Not in your package\n", "text/plain")
            if not self.slot(user, "ch%d.%s" % (sid, ext or "m3u8")):
                return
            if ext == "ts":
                return self.live_ts(user, pw, c, sid)
            return self.live_playlist(c)
        v = CAT.vod_by_id.get(sid)
        if not v or not SECRET:
            return self.send(404, "Not found\n", "text/plain")
        if not vod_ok(acct, v):
            return self.send(403, "Not in your package\n", "text/plain")
        log("play vod %s %s .%s ip=%s ua=%.40s" % (user, v["slug"], ext or "m3u8", self.ip(),
                                                  self.headers.get("User-Agent") or ""))
        if not self.slot(user, "vod.%s" % (ext or "m3u8")):
            return
        if ext in ("", "m3u8"):
            return self.vod_playlist(user, v, base)
        return self.vod_ts(v)

    def live_ts(self, user, pw, c, sid):
        """One endless MPEG-TS stream for apps in TS mode: the channel's HLS segments from
        local disk, back to back as they appear. Ends when the app disconnects, the
        channel stalls for TS_IDLE_MAX, or the account stops being active."""
        fp = local_playlist(c)
        if not fp:
            return self.send(404, "Not found\n", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "close")
        self.end_headers()
        if self.command == "HEAD":
            return
        self.close_connection = True
        self.connection.settimeout(30)              # an app that stops reading frees the thread
        device = device_key(self.ip(), self.headers.get("User-Agent"))
        clock = TsRebase()
        last, t0, sent, why = None, time.time(), 0, "client closed"
        last_new = last_auth = t0
        try:
            while True:
                try:
                    segs = playlist_segments(fp)
                except (OSError, ValueError):
                    segs = []
                if segs:
                    if last is None:
                        last = segs[max(0, len(segs) - TS_START_SEGS)][0] - 1
                    elif segs[-1][0] < last:         # encoder restarted with new numbering
                        last = segs[-1][0] - 1
                    for sq, path in segs:
                        if sq <= last:
                            continue
                        try:
                            with open(path, "rb") as f:
                                data = f.read()
                            # A segment cut off by an encoder restart can end mid-packet;
                            # sent as-is, every later packet in the stream is misaligned
                            # (players lose sync, TsRebase stops seeing packets). Drop
                            # the partial tail so the stream stays 188-byte aligned.
                            chunk = (clock.process(data[:len(data) - len(data) % 188])
                                     if data[:1] == b"\x47" else b"")   # not TS: skip
                        except FileNotFoundError:   # rotated out between read and open
                            chunk = b""
                        self.connection.sendall(chunk)
                        sent += len(chunk)
                        last, last_new = sq, time.time()
                        claim_slot(user, device)    # keep this device's slot warm
                now = time.time()
                if now - last_new > TS_IDLE_MAX:
                    why = "channel stalled"
                    break
                if now - last_auth > TS_REAUTH_SECS:
                    last_auth = now
                    try:
                        a = authenticate(user, pw, self.ip())
                    except PermissionError:
                        a = None
                    if not a or not a["active"]:
                        why = "account no longer active"
                        break
                claim_slot(user, device)
                time.sleep(1)
        except OSError:                             # broken pipe / reset / send timeout
            pass
        log("ts end %s ch%d %ds %.1fMB %d clock rebase(s) (%s)"
            % (user, sid, time.time() - t0, sent / 1e6, clock.rebases, why))

    def live_playlist(self, c):
        """The channel's live playlist from local disk, segment URIs made absolute to the
        CDN — the app polls US (device accounting) but pulls video from the CDN."""
        fp = local_playlist(c)
        cdn_dir = c["hls"].rsplit("/", 1)[0] + "/"
        try:
            if not fp:
                raise OSError("no local playlist")
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


    def vod_local(self, v):
        """(movie folder on disk, its index.m3u8) or (None, None)."""
        root, _, rel = v["key"].partition("/")
        d = os.path.realpath(os.path.join(VOD_ROOTS[root], rel))
        pl = os.path.join(d, "index.m3u8")
        if not d.startswith(os.path.realpath(VOD_ROOTS[root]) + os.sep) or not os.path.isfile(pl):
            return None, None
        return d, pl

    def vod_playlist(self, user, v, base):
        """The movie's playlist served AT the /movie/ URL, every segment an absolute signed
        /xv/ URL. Not a redirect: ExoPlayer-based apps resolve relative segment names
        against the ORIGINAL URL, so after a 302 they'd ask for the wrong address."""
        d, pl = self.vod_local(v)
        if not pl:
            return self.send(404, "Not found\n", "text/plain")
        signed = base + vod_path(user, v["key"]).rsplit("/", 1)[0] + "/"
        out = []
        for line in open(pl, encoding="utf-8", errors="replace").read().splitlines():
            if line and not line.startswith("#"):
                line = urljoin(signed, line)
            elif 'URI="' in line:
                line = re.sub(r'URI="([^"]+)"',
                              lambda m: 'URI="%s"' % urljoin(signed, m.group(1)), line)
            out.append(line)
        self.send(200, "\n".join(out) + "\n", "application/vnd.apple.mpegurl")

    def vod_ts(self, v):
        """The whole movie as ONE MPEG-TS file for players that want a file, not HLS: the
        segments of one encode concatenate into a valid TS. Byte ranges are mapped across
        the segment files, so seeking works."""
        d, pl = self.vod_local(v)
        if not pl:
            return self.send(404, "Not found\n", "text/plain")
        parts = []
        for line in open(pl, encoding="utf-8", errors="replace"):
            line = line.strip()
            if line and not line.startswith("#"):
                fp = os.path.realpath(os.path.join(d, line))
                if fp.startswith(d + os.sep) and os.path.isfile(fp):
                    parts.append((fp, os.path.getsize(fp)))
        total = sum(sz for _, sz in parts)
        start, end = 0, total - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), total - 1) if m.group(2) else total - 1
            else:                                   # suffix range: last N bytes
                start = max(0, total - int(m.group(2)))
            if start > end or start >= total:
                return self.send(416, b"", "text/plain", {"Content-Range": "bytes */%d" % total})
        partial = bool(m and (m.group(1) or m.group(2)))
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if self.command == "HEAD":
            return
        self.connection.settimeout(30)
        pos = 0
        try:
            for fp, sz in parts:
                if pos + sz <= start:
                    pos += sz
                    continue
                if pos > end:
                    break
                off = max(0, start - pos)
                count = min(sz, end - pos + 1) - off
                with open(fp, "rb") as f:
                    self.connection.sendfile(f, off, count)
                pos += sz
        except OSError:                             # app closed / seeked away
            pass

    def vod_image(self, rel):
        root, _, rest = rel.partition("/")
        if root not in VOD_ROOTS or ".." in rest.split("/"):
            return self.send(404, "Not found\n", "text/plain")
        base = os.path.realpath(VOD_ROOTS[root])
        fp = os.path.realpath(os.path.join(base, rest))
        ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".webp": "image/webp"}.get(os.path.splitext(fp)[1].lower())
        if not ctype or not fp.startswith(base + os.sep) or not os.path.isfile(fp):
            return self.send(404, "Not found\n", "text/plain")
        with open(fp, "rb") as f:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(os.fstat(f.fileno()).st_size))
            self.send_header("Cache-Control", "public, max-age=86400")   # same for everyone
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            if self.command != "HEAD":
                self.connection.sendfile(f)

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
