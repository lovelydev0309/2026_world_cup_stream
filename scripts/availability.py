#!/usr/bin/env python3
"""Sustained per-channel availability, measured the way a VIEWER experiences it.

An instantaneous "is it advancing right now" check passes for a channel that is
unwatchable, which is why snapshot checks disagreed with the client's monitoring.
This polls every channel once per round over many rounds and records, per round,
whether a joining player could actually start: playlist fetches, playlist has
advanced since the previous round, and the newest listed segment is fetchable.
Writes a running report so partial results are usable if interrupted.
"""
import json, os, re, subprocess, sys, time

CDN = "https://stream.tv247on.com/hls"
ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
GAP    = int(sys.argv[2]) if len(sys.argv) > 2 else 60
OUT    = "/opt/streaming-stack/logs/availability.json"
os.chdir("/opt/streaming-stack")

names = [c["name"] for c in json.load(open("player/channels.json"))]
title = {c["name"]: c["title"] for c in json.load(open("player/channels.json"))}
state = {n: {"fail": 0, "ok": 0, "why": {}, "lastseq": None} for n in names}

def curl(url, t=8, out="/dev/null"):
    try:
        r = subprocess.run(["curl", "-s", "--max-time", str(t), "-o", out,
                            "-w", "%{http_code}", url], capture_output=True, text=True, timeout=t + 5)
        return r.stdout.strip()
    except Exception:
        return "000"

def body(url, t=8):
    try:
        return subprocess.run(["curl", "-s", "--max-time", str(t), url],
                              capture_output=True, text=True, timeout=t + 5).stdout
    except Exception:
        return ""

for rnd in range(1, ROUNDS + 1):
    t0 = time.time()
    for n in names:
        m = body("%s/%s/index.m3u8" % (CDN, n))
        if not m or "#EXTM3U" not in m:
            state[n]["fail"] += 1; state[n]["why"]["playlist"] = state[n]["why"].get("playlist", 0) + 1; continue
        seq = re.search(r"MEDIA-SEQUENCE:(\d+)", m)
        segs = re.findall(r"^([^#\s].*\.ts)$", m, re.M)
        if not seq or not segs:
            state[n]["fail"] += 1; state[n]["why"]["empty"] = state[n]["why"].get("empty", 0) + 1; continue
        s = int(seq.group(1)); prev = state[n]["lastseq"]; state[n]["lastseq"] = s
        if prev is not None and s <= prev:
            state[n]["fail"] += 1; state[n]["why"]["stalled"] = state[n]["why"].get("stalled", 0) + 1; continue
        code = curl("%s/%s/%s" % (CDN, n, segs[-1]), t=10)
        if code not in ("200", "206"):
            state[n]["fail"] += 1; state[n]["why"]["seg" + code] = state[n]["why"].get("seg" + code, 0) + 1; continue
        state[n]["ok"] += 1
    rep = {"rounds_done": rnd, "of": ROUNDS, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "channels": {n: {"ok": state[n]["ok"], "fail": state[n]["fail"], "why": state[n]["why"]} for n in names}}
    json.dump(rep, open(OUT, "w"), indent=1)
    el = time.time() - t0
    print("[round %d/%d] %.0fs  failing-now=%d" % (rnd, ROUNDS, el,
          sum(1 for n in names if state[n]["fail"] > 0)), flush=True)
    if rnd < ROUNDS and el < GAP:
        time.sleep(GAP - el)

print("\n=== channels with ANY failure over %d rounds ===" % ROUNDS)
bad = sorted([(state[n]["fail"], n) for n in names if state[n]["fail"] > 0], reverse=True)
for f, n in bad:
    print("  %-12s %-28s fail %d/%d  %s" % (n, title[n][:28], f, ROUNDS, state[n]["why"]))
print("\nperfect: %d / %d" % (len(names) - len(bad), len(names)))
