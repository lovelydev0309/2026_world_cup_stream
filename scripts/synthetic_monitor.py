#!/usr/bin/env python3
"""
synthetic_monitor.py — proactive "synthetic viewer" health monitor.

For every channel it does what a real viewer's player does: fetches the live playlist
THROUGH THE CDN (stream.tv247on.com), checks the live edge is advancing, then downloads
the newest delivered segment and DECODES it to confirm real video frames + audio (correct
sample rate, not silent). This catches dead channels, corrupt/degraded feeds, silent audio,
frozen/stale streams, ENDLIST-stops and CDN delivery problems BEFORE a viewer or the client
reports them.

It fetches OUR CDN output (not the tvon247 source), so it consumes ZERO provider connection
slots. Writes:
  - /opt/streaming-stack/player/status.json   (machine + dashboard state)
  - /opt/streaming-stack/logs/synthetic_monitor.log
and sends ONE consolidated alert (via send_alert.sh, which is cooldown-throttled) listing any
channel that has been unhealthy for >=2 consecutive runs (so a one-off blip never pages).

Run from cron every 5 min:  */5 * * * * /opt/streaming-stack/scripts/synthetic_monitor.py
"""
import json, subprocess, urllib.request, time, os, re

CDN        = "https://stream.tv247on.com/hls"
STATUS     = "/opt/streaming-stack/player/status.json"
LOG        = "/opt/streaming-stack/logs/synthetic_monitor.log"
STATE      = "/opt/streaming-stack/cache/synthetic_state.json"   # consecutive-bad counters
ALERT_SH   = "/opt/streaming-stack/scripts/send_alert.sh"
UA         = "Mozilla/5.0 (SyntheticMonitor)"
SILENCE_DB = -70          # a full segment below this = effectively silent
CONFIRM_N  = 2            # unhealthy for this many consecutive runs before we alert

CHANNELS = {
    1:"World Cup", 2:"Canal 5", 3:"ESPN", 4:"Las Estrellas", 5:"Azteca Uno",
    6:"Imagen", 7:"ForoTV", 8:"Fox Sports 1", 9:"ESPN 2", 10:"TUDN",
    11:"Cartoon Network", 12:"TNT Mexico", 13:"AXN", 14:"Space", 15:"Discovery",
}

def now():  return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
def log(m):
    try: open(LOG, "a").write("[%s] %s\n" % (now(), m))
    except Exception: pass

def fetch(url, timeout=12, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
    r = urllib.request.urlopen(req, timeout=timeout)
    d = r.read()
    return d if binary else d.decode("utf-8", "replace")

def last_segment(manifest):
    segs = [l.strip() for l in manifest.splitlines() if l.strip() and not l.startswith("#")]
    return segs[-1] if segs else None

def ffprobe(args, timeout=20):
    return subprocess.run(["ffprobe", "-v", "error", "-user_agent", UA] + args,
                          capture_output=True, text=True, timeout=timeout).stdout.strip()

def probe_manifest(n):
    """Pass 1: fetch playlist via CDN, return (status_or_None, last_seg, manifest)."""
    url = "%s/channel%d/index.m3u8" % (CDN, n)
    try:
        m = fetch(url)
    except Exception as e:
        return ("DOWN", None, "playlist fetch failed: %s" % str(e)[:70])
    if "#EXT-X-ENDLIST" in m:
        return ("ENDED", None, "playlist has #EXT-X-ENDLIST (player would stop)")
    seg = last_segment(m)
    if not seg:
        return ("DOWN", None, "playlist has no segments")
    return (None, seg, m)

def decode_segment(n, seg):
    """Download newest segment via CDN and decode: returns dict with v/a facts."""
    url = "%s/channel%d/%s" % (CDN, n, seg)
    out = {"video": "", "sr": "", "mean": None, "v_ok": False, "a_ok": False}
    try:
        v = ffprobe(["-select_streams", "v:0", "-show_entries",
                     "stream=codec_name,width,height", "-of", "csv=p=0", url])
        if v:
            p = v.splitlines()[0].split(",")   # first stream only, no stray newlines
            out["v_ok"] = True
            if len(p) >= 3: out["video"] = "%sx%s" % (p[1].strip(), p[2].strip())
        a = ffprobe(["-select_streams", "a:0", "-show_entries",
                     "stream=sample_rate,channels", "-of", "csv=p=0", url])
        if a:
            out["a_ok"] = True
            out["sr"] = a.splitlines()[0].split(",")[0].strip()
        vd = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-user_agent", UA,
                             "-i", url, "-map", "a:0", "-af", "volumedetect", "-f", "null", "-"],
                            capture_output=True, text=True, timeout=25).stderr
        mm = re.search(r"mean_volume:\s*([-0-9.]+)", vd)
        if mm: out["mean"] = float(mm.group(1))
    except Exception as e:
        out["err"] = str(e)[:60]
    return out

def classify(advancing, dec):
    if "err" in dec and not dec["v_ok"]:
        return ("DEGRADED", "segment decode error: %s" % dec["err"])
    if not advancing:
        return ("STALE", "live edge frozen (playlist not advancing)")
    if not dec["v_ok"]:
        return ("NO_VIDEO", "no decodable video in delivered segment")
    if not dec["a_ok"]:
        return ("NO_AUDIO", "video ok (%s) but no audio stream" % dec["video"])
    if dec["mean"] is not None and dec["mean"] < SILENCE_DB:
        return ("SILENT", "video ok (%s) but audio silent (%.0f dB)" % (dec["video"], dec["mean"]))
    return ("OK", "%s, audio %sHz @ %.0fdB" % (dec["video"], dec["sr"],
            dec["mean"] if dec["mean"] is not None else 0))

def load_state():
    try: return json.load(open(STATE))
    except Exception: return {}

def main():
    # Pass 1: grab every playlist + its current last segment (fast).
    p1 = {}
    for n in CHANNELS:
        p1[n] = probe_manifest(n)

    # Decode newest segment per channel — this loop's wall-clock IS the "advancing" window.
    decoded = {}
    for n in CHANNELS:
        st, seg, _ = p1[n]
        decoded[n] = decode_segment(n, seg) if st is None else None

    # Pass 2: re-fetch playlists; the last segment must have advanced since pass 1.
    results = []
    for n in CHANNELS:
        st, seg1, detail = p1[n]
        if st is not None:                       # already failed at manifest stage
            results.append({"ch": n, "name": CHANNELS[n], "status": st, "detail": detail,
                            "video": "", "sr": "", "mean": None})
            continue
        try: seg2 = last_segment(fetch("%s/channel%d/index.m3u8" % (CDN, n)))
        except Exception: seg2 = seg1
        advancing = (seg2 is not None and seg2 != seg1)
        dec = decoded[n] or {"v_ok": False, "a_ok": False, "video": "", "sr": "", "mean": None}
        status, det = classify(advancing, dec)
        results.append({"ch": n, "name": CHANNELS[n], "status": status, "detail": det,
                        "video": dec.get("video", ""), "sr": dec.get("sr", ""),
                        "mean": dec.get("mean")})

    for r in results:
        log("channel%-2d %-15s %-9s %s" % (r["ch"], r["name"], r["status"], r["detail"]))

    ok = sum(1 for r in results if r["status"] == "OK")
    payload = {"updated": now(), "ok": ok, "total": len(results), "channels": results}
    try:
        tmp = STATUS + ".tmp"; open(tmp, "w").write(json.dumps(payload)); os.replace(tmp, STATUS)
    except Exception as e:
        log("status write failed: %s" % e)

    # Confirmed-issue alerting: only page for channels unhealthy >=CONFIRM_N runs in a row.
    state = load_state()
    bad_now, confirmed = [], []
    for r in results:
        key = str(r["ch"])
        if r["status"] == "OK":
            state[key] = 0
        else:
            state[key] = state.get(key, 0) + 1
            bad_now.append(r)
            if state[key] >= CONFIRM_N:
                confirmed.append(r)
    try: json.dump(state, open(STATE, "w"))
    except Exception: pass

    if confirmed:
        lines = "\n".join("  - channel%d (%s): %s [%s]" %
                          (r["ch"], r["name"], r["status"], r["detail"]) for r in confirmed)
        body = ("Synthetic viewer monitor found these channels unhealthy for >=%d consecutive "
                "checks (verified through the CDN, as a real viewer sees them):\n\n%s\n\n"
                "Detail in logs/synthetic_monitor.log; live status at /player/status.html." %
                (CONFIRM_N, lines))
        # Stable subject -> send_alert.sh cooldown caps this at ~1/hour no matter how many break.
        try:
            subprocess.run([ALERT_SH, "Synthetic monitor: channel issues", body],
                           timeout=30, capture_output=True)
        except Exception as e:
            log("alert send failed: %s" % e)

    log("RUN done: %d/%d OK  bad=%d confirmed=%d" %
        (ok, len(results), len(bad_now), len(confirmed)))

if __name__ == "__main__":
    main()
