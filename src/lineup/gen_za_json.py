#!/usr/bin/env python3
# Copyright (c) 2026 lovelydev0309. All rights reserved.
# Proprietary and confidential. See LICENSE. Unauthorized copying, modification or use is prohibited.
"""Write player/channels_za.json: the South Africa lineup for the mzolotv.com site.

That file was written by hand once (2026-10-22) and never again, so after the ZA lineup moved
to tvon247 and nine channels were swapped (2026-10-26/27) it still listed the old names
("SuperSport Extra FHD", "eTV FHD", "kykNET FHD", ...) and two channels that are off. The site's
developer reads it, so the site disagreed with what we serve. Cron runs this every minute.

Unlike player/channels.json this list is NOT health-pruned: every enabled, non-hidden ZA channel
is listed, with `status` "live" when live.json says it is watchable and "starting" otherwise.
A stable list is what a site menu needs; the status field carries the health.
"""
import json, os, time

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))   # src/<group>/x.py -> repo root
CFG = os.path.join(PROJECT, 'config', 'channels.json')
LIVE = os.path.join(PROJECT, 'player', 'live.json')
OUT = os.path.join(PROJECT, 'player', 'channels_za.json')
BASE = 'https://stream.tv247on.com'
LIVE_MAX_AGE = 60


def live_states():
    try:
        d = json.load(open(LIVE))
        age = time.time() - time.mktime(time.strptime(d['updated'], '%Y-%m-%dT%H:%M:%SZ'))
        if abs(age) > LIVE_MAX_AGE:
            return None
        return {c['name']: c.get('state') for c in d.get('channels', [])}
    except Exception:
        return None


def absolute(path):
    return path if path.startswith('http') else BASE + path


def versioned(url):
    """Append ?v=<file mtime>. The site keeps whatever image it fetched for a URL (2026-10-27:
    it showed the old e.tv/kykNET/Studio Universal art on the swapped slots although we served
    the new files), so a changed logo needs a changed URL. The mtime changes with the file."""
    if not url.startswith(BASE + '/player/'):
        return url
    try:
        return '%s?v=%d' % (url, os.path.getmtime(os.path.join(PROJECT, url[len(BASE) + 1:])))
    except OSError:
        return url


def main():
    states = live_states()
    out = []
    for ch in json.load(open(CFG)).get('channels', []):
        if ch.get('country') != 'South Africa' or not ch.get('enabled', True) or ch.get('hidden'):
            continue
        name = ch['channel_name']
        logo = absolute(ch.get('logo', '/player/logos/%s.png' % name))
        out.append({
            'name': name,
            'title': ch.get('display_name', name),
            'country': 'South Africa',
            'logo': versioned(logo),
            'logo_light': versioned(logo[:-4] + '_light.png' if logo.endswith('.png') else logo),
            'hls': '%s/hls/%s/index.m3u8' % (BASE, name),
            # health unknown (live.json stale) -> say live rather than mark everything down
            'status': 'live' if states is None or states.get(name) == 'LIVE' else 'starting',
        })
    tmp = '%s.%d.tmp' % (OUT, os.getpid())
    with open(tmp, 'w') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)


if __name__ == '__main__':
    main()
