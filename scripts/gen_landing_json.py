#!/usr/bin/env python3
"""Generate the PUBLIC, sanitized channel list the landing page renders from.

The real config/channels.json holds IPTV account credentials in source_url /
source_urls, so it must NEVER be web-exposed. This emits player/channels.json
with only public fields (title, logo, hls, page) for every enabled channel, so
index.html can build its cards dynamically — new channels appear automatically.

Run on a 1-min cron; also called after any channel add.
"""
import json, os

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(PROJECT, 'config', 'channels.json')
OUT = os.path.join(PROJECT, 'player', 'channels.json')
BASE = 'https://live3.mzolotv.com'

cfg = json.load(open(CFG))
out = []
for ch in cfg.get('channels', []):
    if not ch.get('enabled', True):
        continue
    name = ch['channel_name']
    out.append({
        'name':  name,
        'title': ch.get('display_name', name),
        'logo':  ch.get('logo', f'/player/logos/{name}.png'),
        'hls':   ch.get('hls_url', f'{BASE}/hls/{name}/index.m3u8'),
        'page':  f'/player/{name}.html',
    })

tmp = OUT + '.tmp'
with open(tmp, 'w') as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
os.replace(tmp, OUT)   # atomic swap so a fetch never sees a half-written file
