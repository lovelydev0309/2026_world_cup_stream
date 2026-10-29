#!/usr/bin/env python3
# Copyright (c) 2026 lovelydev0309. All rights reserved.
# Proprietary and confidential. See LICENSE. Unauthorized copying, modification or use is prohibited.
"""One-shot / cron guard: never leave an audio-only playlist on the CDN.

Root cause (Imagen 2026-09-09): ffmpeg kept publishing AAC-only segments into an
append_list HLS playlist that still advertised video. hls.js then looped
mediaError/bufferAppendError at 0×0.

This script is a backstop if an encoder or a leftover writer slips. It does not
touch provider credentials. Safe to run every minute under flock.
"""
import json, os, re, signal, subprocess, sys

PROJECT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))   # src/<group>/x.py -> repo root
CONFIG = os.path.join(PROJECT, "config", "channels.json")
HLS = os.path.join(PROJECT, "hls")
MIN_VIDEO = 200_000


def cmdline(pid):
    try:
        return open("/proc/%s/cmdline" % pid, "rb").read().replace(b"\0", b" ").decode()
    except Exception:
        return ""


def ffmpeg_for(channel):
    out = []
    needle = "hls/%s/" % channel
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        cmd = cmdline(pid)
        if "ffmpeg" in cmd and needle in cmd:
            out.append(int(pid))
    return out


def completed_seg(channel):
    d = os.path.join(HLS, channel)
    if not os.path.isdir(d):
        return None
    segs = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".ts") or f.endswith(".m4s")]
    if len(segs) < 2:
        return None
    segs.sort(key=os.path.getmtime)
    return segs[-2]


def has_video(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=8)
        return bool((r.stdout or "").strip())
    except Exception:
        return False


def wipe(channel):
    d = os.path.join(HLS, channel)
    if not os.path.isdir(d):
        return
    for f in os.listdir(d):
        if f.endswith((".ts", ".m4s", ".m3u8")) or f == "init.mp4":
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass


def main():
    cfg = json.load(open(CONFIG))
    bad = []
    extras = []
    for c in cfg.get("channels", []):
        if not c.get("enabled", True) or c.get("quarantined"):
            continue
        name = c["channel_name"]
        ffs = ffmpeg_for(name)
        keep = None
        try:
            keep = int(open("/tmp/ffmpeg_%s.pid" % name).read().strip())
        except Exception:
            pass
        if len(ffs) > 1:
            extras.append((name, ffs, keep))
            live = None
            for pid in ffs:
                cmd = cmdline(str(pid))
                if "ffmpeg" in cmd and "standby" not in cmd:
                    live = pid
            if live:
                keep = live
            for pid in ffs:
                if keep and pid == keep:
                    continue
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        seg = completed_seg(name)
        if not seg:
            continue
        age = os.path.getmtime(seg)
        import time
        if time.time() - age > 40:
            continue
        sz = os.path.getsize(seg)
        # Size alone is not enough: the standby slate is ~36KB but HAS video.
        # Only wipe when ffprobe finds no video stream (the Imagen 0×0 case).
        if not has_video(seg):
            bad.append((name, c.get("display_name"), sz, os.path.basename(seg)))
            wipe(name)
            for pid in ffs:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    if extras:
        print("reaped extra ffmpeg:", [(n, len(p)) for n, p, _ in extras])
    if bad:
        print("wiped novideo:", bad)
        return 2
    print("ok: no audio-only playlists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
