#!/usr/bin/env python3
"""Generate the PUBLIC, sanitized channel list the landing page renders from.

The real config/channels.json holds IPTV account credentials in source_url /
source_urls, so it must NEVER be web-exposed. This emits player/channels.json
with only public fields (name, title, logo [absolute URL], hls) for every enabled
channel, so index.html — and external sites — can build lists dynamically; new
channels appear automatically. The per-channel player page is derived from `name`.

LIVE HEALTH (2026-09-15): the lineup used to be health-blind — a channel that had
been stalled for an hour still appeared, so viewers picked it and got nothing while
status.html had known for seconds. Each entry now carries `live` and `state` from
player/live.json (refreshed every 5s by live_status.py), and a channel that stays
unhealthy is dropped from the list entirely.

Three guards stop that from doing more harm than the problem it fixes:

  * GRACE — a channel must be unhealthy CONTINUOUSLY for DELIST_AFTER seconds before
    it is dropped. Producers restart on every provider token expiry (~90s), so a
    channel is briefly STALLED many times an hour; delisting on the instantaneous
    reading would make the client's channel list flicker constantly. Coming back is
    immediate — one healthy reading relists it.
  * SYSTEMIC — if more than MAX_DELIST_FRACTION of channels look unhealthy at once,
    that is our fault (CPU, disk, a bad rollout), not N independent provider
    outages. Delisting is suspended wholesale rather than emptying the client's
    lineup during an incident.
  * STALE — if live.json is missing or older than LIVE_MAX_AGE, health is unknown,
    so every channel is listed as before. A broken monitor must never be able to
    take channels off the air.

Run from cron every minute AND from the live-status 5s loop, so the lineup tracks
health in about five seconds rather than up to a minute.
"""
import json, os, time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(PROJECT, 'config', 'channels.json')
OUT = os.path.join(PROJECT, 'player', 'channels.json')
LIVE = os.path.join(PROJECT, 'player', 'live.json')
HEALTH = os.path.join(PROJECT, 'cache', 'lineup_health.json')
BASE = 'https://stream.tv247on.com'

DELIST_AFTER = 180          # seconds continuously unhealthy before dropping from the lineup
LIVE_MAX_AGE = 60           # live.json older than this = health unknown, list everything
MAX_DELIST_FRACTION = 0.25  # more than this looking bad at once = systemic, suspend delisting
HEALTHY = ('LIVE',)         # SLATE/SLOW/STALLED/OFF all count as not watchable


def country_of(name):
    """Country for filtering. Uses the channel's explicit `country` field if set,
    else derives from the channel number: 1-15 Mexico, 16-27 Peru, 28+ US."""
    try:
        n = int(''.join(filter(str.isdigit, name)))
    except ValueError:
        return ''
    return 'Mexico' if n <= 15 else 'Peru' if n <= 27 else 'US'


def load_live():
    """(state_by_channel, usable). usable is False whenever we cannot trust the data."""
    try:
        d = json.load(open(LIVE))
        age = time.time() - time.mktime(time.strptime(d['updated'], '%Y-%m-%dT%H:%M:%SZ'))
        if age > LIVE_MAX_AGE or age < -LIVE_MAX_AGE:
            return {}, False
        return {c['name']: c.get('state') for c in d.get('channels', [])}, True
    except Exception:
        return {}, False


def load_health():
    try:
        return json.load(open(HEALTH))
    except Exception:
        return {}


def save_health(h):
    try:
        os.makedirs(os.path.dirname(HEALTH), exist_ok=True)
        tmp = '%s.%d.tmp' % (HEALTH, os.getpid())
        with open(tmp, 'w') as f:
            json.dump(h, f)
        os.replace(tmp, HEALTH)
    except OSError:
        pass


cfg = json.load(open(CFG))
states, live_ok = load_live()
health = load_health()
now = time.time()

# Candidates are everything the operator has switched on; health only prunes this set.
candidates = []
for ch in cfg.get('channels', []):
    if not ch.get('enabled', True):
        continue
    # 'hidden' is PRESENTATION ONLY: the channel keeps running, keeps its sources and keeps
    # serving its HLS url -- it is simply not advertised in the public lineup. This is NOT
    # 'enabled', which stops the producer entirely. Use it to pull a channel from what the
    # client shows viewers without tearing down a working stream; set it back to false to
    # list the channel again.
    if ch.get('hidden'):
        continue
    # Quarantine (provider-dead auto-hide) is also honoured when that tooling is present.
    if ch.get('quarantined'):
        continue
    candidates.append(ch)

# Track how long each candidate has been unhealthy, and decide who is droppable.
droppable, fresh = set(), {}
for ch in candidates:
    name = ch['channel_name']
    st = states.get(name)
    if not live_ok or st is None:
        fresh[name] = None                       # unknown health -> never counts against it
        continue
    if st in HEALTHY:
        fresh[name] = None
        continue
    since = health.get(name) or now
    fresh[name] = since
    if now - since >= DELIST_AFTER:
        droppable.add(name)

# Systemic guard: an incident on our side must not strip the client's lineup.
suspend = live_ok and candidates and len(droppable) > MAX_DELIST_FRACTION * len(candidates)

out = []
for ch in candidates:
    name = ch['channel_name']
    if name in droppable and not suspend:
        continue
    logo = ch.get('logo', f'/player/logos/{name}.png')
    if logo.startswith('/'):          # emit an absolute URL so external sites can use it directly
        logo = BASE + logo
    # A channel running a stand-in is labelled, so the client and their viewers can see
    # what is actually on air. Silently passing a substitute off as the real channel would
    # mean nobody ever chases the provider for the real feed.
    title = ch.get('display_name', name)
    sub = ch.get('substituted')
    if sub:
        title = '%s · temporarily %s' % (title, sub.get('label', 'alternate channel'))
    entry = {
        'name':    name,
        'title':   title,
        'country': ch.get('country') or country_of(name),
        'logo':    logo,
        'hls':     ch.get('hls_url', f'{BASE}/hls/{name}/index.m3u8'),
    }
    # Additive fields: existing consumers ignore them, new ones can grey out a bad channel
    # in real time instead of waiting for it to drop out three minutes later.
    if live_ok and states.get(name) is not None:
        entry['state'] = states[name]
        entry['live'] = states[name] in HEALTHY
    out.append(entry)

save_health({k: v for k, v in fresh.items() if v is not None})

tmp = '%s.%d.tmp' % (OUT, os.getpid())   # pid-unique: cron and the 5s loop can overlap
with open(tmp, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
os.replace(tmp, OUT)   # atomic swap so a fetch never sees a half-written file
