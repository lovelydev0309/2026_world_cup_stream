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
    # Quarantined = the provider stopped broadcasting this feed, so it is hidden from the
    # client lineup until it comes back (scripts/quarantine_manager.py sets/clears the flag).
    # Showing a dead channel is worse than showing none: viewers get a standby slate and
    # think the product is broken. This is SEPARATE from `enabled`, which is the human switch.
    if ch.get('quarantined'):
        continue
    name = ch['channel_name']
    logo = ch.get('logo', f'/player/logos/{name}.png')
    if logo.startswith('/'):          # emit an absolute URL so external sites can use it directly
        logo = BASE + logo
    out.append({
        'name':    name,
        'title':   ch.get('display_name', name),
        'country': ch.get('country') or country_of(name),
        'logo':    logo,
        'hls':     ch.get('hls_url', f'{BASE}/hls/{name}/index.m3u8'),
    })

tmp = OUT + '.tmp'
with open(tmp, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
os.replace(tmp, OUT)   # atomic swap so a fetch never sees a half-written file
