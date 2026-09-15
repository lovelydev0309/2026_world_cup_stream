#!/usr/bin/env python3
"""Real-time liveness for every published channel -> player/live.json.

This is the FAST tier of a two-tier dashboard. synthetic_monitor.py is the slow tier: it
pulls a segment per channel THROUGH THE CDN and decodes it, which is the only way to prove
picture and sound actually reach a viewer — but 77 channels of that costs ~4.5 min a pass,
so it can never be real time.

This script answers the cheaper question — "is the channel putting segments out RIGHT NOW?"
— purely from the local HLS directory: no provider connections, no CDN fetches, no decode.
It is a few hundred stat() calls, so it can run every few seconds without costing anything.

State per channel:
  LIVE     segments are landing and the playlist is advancing at roughly realtime
  SLATE    producing, but the last transition in the log was -> STANDBY, so viewers are
           seeing the standby slate, not the real programme. Counting this as "up" is what
           makes a dashboard say everything is fine while viewers watch a holding card.
  SLOW     advancing, but well under realtime — the encoder is behind, viewers will rebuffer
  STALLED  playlist present but the newest segment has aged out past 3x target duration
  OFF      no playlist or no segments at all

Advancement is measured against the PREVIOUS run's snapshot (cache/live_state.json), using
each channel's OWN EXT-X-TARGETDURATION — segment length varies per channel (2s vs 4s), and
assuming a fixed 4 makes half the fleet read as 2x realtime.
"""
import json, os, re, time, glob

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HLS = os.path.join(PROJECT, "hls")
LOGS = os.path.join(PROJECT, "logs")
LINEUP = os.path.join(PROJECT, "player", "channels.json")
OUT = os.path.join(PROJECT, "player", "live.json")
STATE = os.path.join(PROJECT, "cache", "live_state.json")

STALL_FACTOR = 3        # newest segment older than this many target-durations => STALLED
STALL_FLOOR = 12        # ...but never below this many seconds: 3x a 2s segment is only 6s,
                        # which flags an ordinary token-refresh reconnect as a stall.
SLOW_RATIO = 0.55       # advancing slower than this fraction of realtime => SLOW
RATE_BASELINE = 30      # seconds of history a rate is measured over (see below)

# Freshness (age) is sampled every run, but ADVANCEMENT cannot be: over a 5s interval a
# 4s-segment channel advances 0, 1 or 2 segments, so the rate quantises to 0.0 / 0.8 / 1.6
# and "SLOW" fires on pure sampling noise. So the rate is measured against a baseline at
# least RATE_BASELINE seconds old (~7 segments, fine resolution) and carried forward between
# baseline rolls, while age keeps updating every 5s.


def read_playlist(ch):
    p = os.path.join(HLS, ch, "index.m3u8")
    if not os.path.isfile(p):
        return None
    try:
        t = open(p).read()
    except OSError:
        return None
    seq = re.search(r"MEDIA-SEQUENCE:(\d+)", t)
    td = re.search(r"EXT-X-TARGETDURATION:(\d+)", t)
    return {
        "seq": int(seq.group(1)) if seq else None,
        "td": int(td.group(1)) if td else 4,
        "count": len(re.findall(r"^[^#\s].*\.ts$", t, re.M)),
    }


def newest_age(ch, now):
    d = os.path.join(HLS, ch)
    newest = 0.0
    try:
        with os.scandir(d) as it:
            for e in it:
                if e.name.endswith(".ts"):
                    m = e.stat().st_mtime
                    if m > newest:
                        newest = m
    except OSError:
        return None
    return (now - newest) if newest else None


def last_transition(ch):
    """Most recent '-> LIVE' / '-> STANDBY' from the channel log, read from the tail only."""
    p = os.path.join(LOGS, "%s.log" % ch)
    try:
        size = os.path.getsize(p)
        with open(p, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    mode = None
    for line in tail.splitlines():
        if "→ STANDBY" in line:
            mode = "standby"
        elif "→ LIVE" in line:
            mode = "live"
    return mode


def main():
    try:
        lineup = json.load(open(LINEUP))
    except Exception:
        lineup = []
    names = [c["name"] for c in lineup] or [
        os.path.basename(p.rstrip("/")) for p in glob.glob(os.path.join(HLS, "channel*/"))
    ]
    title = {c["name"]: c.get("title", c["name"]) for c in lineup}
    country = {c["name"]: c.get("country", "") for c in lineup}

    try:
        prev = json.load(open(STATE))
    except Exception:
        prev = {}
    prev_ch = prev.get("channels", {})
    base = prev.get("base", {})
    base_ch = base.get("channels", {})
    base_at = base.get("at", 0)

    now = time.time()
    dt = now - base_at if base_at else 0
    roll = dt >= RATE_BASELINE          # baseline old enough to measure against, then reset
    out, state = [], {}
    counts = {"LIVE": 0, "SLATE": 0, "SLOW": 0, "STALLED": 0, "OFF": 0}

    for ch in names:
        pl = read_playlist(ch)
        age = newest_age(ch, now)
        num = int(re.sub(r"\D", "", ch) or 0)

        if not pl or pl["seq"] is None or age is None:
            st, rate = "OFF", None
        else:
            td = max(pl["td"], 1)
            # Carry the last computed rate forward until the baseline rolls, so the value
            # on screen is stable rather than flickering with sampling noise.
            rate = (prev_ch.get(ch) or {}).get("rate")
            if roll:
                b = base_ch.get(ch)
                # A restart resets MEDIA-SEQUENCE, so a negative delta means "no measurement"
                # rather than "slow" -- drop the rate instead of reporting a false 0.
                rate = None
                if b is not None and pl["seq"] is not None:
                    d = pl["seq"] - b
                    if d >= 0:
                        rate = (d * td) / dt

            if age > max(td * STALL_FACTOR, STALL_FLOOR):
                st = "STALLED"
            elif last_transition(ch) == "standby":
                st = "SLATE"
            elif (rate is not None and rate < SLOW_RATIO
                  and age > td and pl["seq"] > 0):
                # Three guards, because a freshly (re)started channel looks slow when it is
                # not: MEDIA-SEQUENCE sits at 0 until the window fills and the first segments
                # roll off, so the delta is legitimately 0 while segments land normally.
                #   seq > 0   the window has actually rolled, so the delta means something
                #   age > td  a segment landed within one segment-duration IS realtime,
                #             whatever the sequence says
                st = "SLOW"
            else:
                st = "LIVE"

        counts[st] = counts.get(st, 0) + 1
        out.append({
            "name": ch,
            "ch": num,
            "title": title.get(ch, ch),
            "country": country.get(ch, ""),
            "state": st,
            "age": round(age, 1) if age is not None else None,
            "rate": round(rate, 2) if rate is not None else None,
            "seg": pl["td"] if pl else None,
            "window": (pl["td"] * pl["count"]) if pl else None,
        })
        state[ch] = {"seq": pl["seq"] if pl else None,
                     "rate": round(rate, 2) if rate is not None else None}

    out.sort(key=lambda c: c["ch"])
    doc = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "interval": round(dt, 1),
        "rate_window": RATE_BASELINE,
        "counts": counts,
        "up": counts["LIVE"],
        "total": len(out),
        "channels": out,
    }

    new_base = ({"at": now, "channels": {c: v["seq"] for c, v in state.items()}}
                if roll or not base_at else base)
    for path, data in ((OUT, doc),
                       (STATE, {"at": now, "channels": state, "base": new_base})):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)          # atomic: a fetch never sees a half-written file
        except OSError:
            pass


if __name__ == "__main__":
    main()
