#!/usr/bin/env python3
"""Generate the PUBLIC, sanitized channel list the landing page renders from.

The real config/channels.json holds IPTV account credentials in source_url /
source_urls, so it must NEVER be web-exposed. This emits player/channels.json
with only public fields (name, title, logo [absolute URL], hls) for every enabled
channel, so index.html — and external sites — can build lists dynamically; new
channels appear automatically. The per-channel player page is derived from `name`.

Run on a 1-min cron; also called after any channel add.
"""
import json, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(PROJECT, 'config', 'channels.json')
OUT = os.path.join(PROJECT, 'player', 'channels.json')
BASE = 'https://stream.tv247on.com'

def country_of(name):
    """Country for filtering. Uses the channel's explicit `country` field if set,
    else derives from the channel number: 1-15 Mexico, 16-27 Peru, 28+ US."""
    try:
        n = int(''.join(filter(str.isdigit, name)))
    except ValueError:
        return ''
    return 'Mexico' if n <= 15 else 'Peru' if n <= 27 else 'US'

cfg = json.load(open(CFG))
out = []
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
    name = ch['channel_name']
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
    out.append({
        'name':    name,
        'title':   title,
        'country': ch.get('country') or country_of(name),
        'logo':    logo,
        'hls':     ch.get('hls_url', f'{BASE}/hls/{name}/index.m3u8'),
    })

tmp = OUT + '.tmp'
with open(tmp, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
os.replace(tmp, OUT)   # atomic swap so a fetch never sees a half-written file
