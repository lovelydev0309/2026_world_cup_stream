#!/usr/bin/env python3
"""
quarantine_manager.py — auto-hide provider-dead channels from the client, auto-restore them.

WHY
---
When a provider feed dies, the channel used to stay in the published lineup and show a
"please stand by" slate to viewers. During a promotion that looks like a broken product.
This script takes such a channel OUT of the client-facing list until the provider is
actually broadcasting again, then puts it straight back.

Removing a dead channel also FREES its provider connection slot and its transcode CPU,
which is exactly the pressure that makes the *healthy* channels flap. So quarantining a
dead feed measurably helps the ones that still work.

PROVIDER-SIDE ONLY
------------------
A channel is quarantined ONLY when the failure is proven to be upstream: after it has been
DOWN for QUARANTINE_AFTER consecutive runs we PROBE ITS ACTUAL SOURCES. If no source
delivers bytes, the provider is at fault -> quarantine. If a source DOES deliver bytes,
the fault is ours (CPU starvation, disk, encoder, CDN) -> we never quarantine, we alert,
because hiding the channel would bury OUR bug instead of fixing it.

SAFETY RAILS (this edits the live config, so it is deliberately timid)
---------------------------------------------------------------------
* Systemic-outage guard: if more than SYSTEMIC_FRACTION of channels are DOWN at once, the
  cause is almost certainly on our side (CDN, box, disk). Quarantine is suspended entirely
  and we alert instead. Prevents a box hiccup from emptying the client's whole lineup.
* MAX_QUARANTINED caps how many channels can ever be hidden at once.
* Never touches "enabled" — that is the human switch. Quarantine is a SEPARATE field, so a
  channel a person deliberately turned off is never auto-resurrected.
* Probes are serialized and budget-capped per run, so we never blow a provider connection
  cap while testing (an over-cap probe returns ~0 bytes and would look like a dead feed).
* Config writes are atomic (tmp + os.replace) because run_channel.sh reads this file
  constantly; a half-written config would take every channel down.
* flock so two runs never overlap.

STATE
-----
The quarantine flag lives on the channel in config/channels.json:

    "quarantined": {
        "since":      "2026-09-07T04:10:00Z",
        "reason":     "provider-side: no source delivered bytes (6 sources probed)",
        "last_probe": "2026-09-07T04:40:00Z",
        "good_probes": 0
    }

Presence of the key = hidden from clients and not produced. Absence = normal.
Streak counters live in cache/quarantine_state.json (not the config) so the config stays
clean and reviewable.

CRON
----
    */5 * * * * flock -n /tmp/quarantine.lock /opt/streaming-stack/scripts/quarantine_manager.py

Flags:
    --dry-run   print what would change, write nothing (use this first, always)
    --status    show current quarantine state and exit
    --release C release channelC manually (e.g. --release channel28)
"""
import json, os, subprocess, sys, time, fcntl

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(PROJECT, "config", "channels.json")
ACCOUNTS = os.path.join(PROJECT, "config", "accounts.env")
STATUS = os.path.join(PROJECT, "player", "status.json")
STATE = os.path.join(PROJECT, "cache", "quarantine_state.json")
LOG = os.path.join(PROJECT, "logs", "quarantine_manager.log")
GEN_JSON = os.path.join(PROJECT, "scripts", "gen_landing_json.py")
ALERT_SH = os.path.join(PROJECT, "scripts", "send_alert.sh")

# --- tuning -----------------------------------------------------------------
QUARANTINE_AFTER = 3        # consecutive runs DOWN before we even consider quarantine
RESTORE_GOOD_PROBES = 3     # 3 consecutive good probes (~15 min sustained) — 1 let intermittent feeds flap back on
PROBE_BUDGET = 15           # probe every hidden channel each run so restores are not delayed
PROBE_TIMEOUT = 12          # seconds per probe
PROBE_MIN_BYTES = 300_000   # a real feed pushes MBs in 12s; an over-cap/dead one gives ~0
MAX_PROBE_SOURCES = 3       # how many of a channel's sources to try before calling it dead
SYSTEMIC_FRACTION = 0.25    # >25% of channels DOWN => our problem, suspend quarantine
MAX_QUARANTINED = 30        # never hide more than this many channels at once
UA = "okhttp/4.9.3"         # provider is UA-filtered; must match what run_channel.sh sends


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def log(msg):
    line = "[%s] %s" % (now(), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json_atomic(path, data):
    """Atomic write — run_channel.sh reads the config constantly; a torn file is an outage."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_accounts():
    """@@ALIAS@@ -> host/user/pass, same resolution run_channel.sh performs."""
    acct = {}
    try:
        with open(ACCOUNTS) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.replace("_", "").isalnum():
                    acct[k.strip()] = v.strip()
    except Exception as e:
        log("WARN cannot read accounts.env: %s" % e)
    return acct


def resolve(url, acct):
    for k, v in acct.items():
        url = url.replace("@@%s@@" % k, v)
    return url


def probe(url, acct):
    """Pull a source briefly and measure bytes. Returns (bytes, resolved_ok).

    This is the ONLY place we touch the provider, and it costs one connection slot for
    PROBE_TIMEOUT seconds — hence the per-run budget and strict serialization.
    """
    real = resolve(url, acct)
    if "@@" in real:
        return 0, False  # unresolved alias — missing credential, not a provider fault
    try:
        out = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-A", UA, "-L",
             "--max-time", str(PROBE_TIMEOUT), "-w", "%{size_download}", real],
            capture_output=True, text=True, timeout=PROBE_TIMEOUT + 8).stdout.strip()
        return int(out or 0), True
    except Exception:
        return 0, True


def sources_of(ch):
    return ch.get("source_urls") or ([ch["source_url"]] if ch.get("source_url") else [])


def alert(subject, body):
    try:
        subprocess.run([ALERT_SH, subject, body], timeout=30, capture_output=True)
    except Exception as e:
        log("alert send failed: %s" % e)


def stop_producer(name):
    """A quarantined channel must stop producing, or it keeps burning a slot and CPU.
    SIGTERM (not -9) so run_channel.sh's trap runs and releases its flock cleanly."""
    try:
        subprocess.run(["pkill", "-TERM", "-f", "run_channel.sh %s$" % name],
                       timeout=15, capture_output=True)
    except Exception as e:
        log("WARN could not stop producer for %s: %s" % (name, e))


def regen_lineup():
    """Republish the client-facing list so the hidden channel disappears immediately."""
    try:
        subprocess.run([sys.executable, GEN_JSON], timeout=60, capture_output=True)
    except Exception as e:
        log("WARN gen_landing_json failed: %s" % e)


def main():
    dry = "--dry-run" in sys.argv
    cfg = load_json(CONFIG)
    if not cfg or "channels" not in cfg:
        log("FATAL cannot read config")
        return 1

    chans = cfg["channels"]
    by_name = {c["channel_name"]: c for c in chans}

    if "--status" in sys.argv:
        q = [c for c in chans if c.get("quarantined")]
        print("quarantined: %d" % len(q))
        for c in q:
            info = c["quarantined"]
            print("  %-12s %-26s since=%s good_probes=%s\n      reason: %s"
                  % (c["channel_name"], c.get("display_name", ""), info.get("since"),
                     info.get("good_probes", 0), info.get("reason")))
        return 0

    if "--release" in sys.argv:
        target = sys.argv[sys.argv.index("--release") + 1]
        ch = by_name.get(target)
        if not ch or not ch.get("quarantined"):
            print("%s is not quarantined" % target)
            return 1
        ch.pop("quarantined")
        save_json_atomic(CONFIG, cfg)
        regen_lineup()
        log("MANUAL RELEASE %s — back in the lineup" % target)
        return 0

    status = load_json(STATUS, {})
    state = load_json(STATE, {}) or {}
    streaks = state.get("down_streaks", {})
    acct = load_accounts()

    # status.json is keyed by channel NUMBER; map it back to channel_name.
    down_now = set()
    for row in status.get("channels", []):
        # raw_status DOWN but display OK == "recovering"; treat as still down for streaks
        bad = row.get("status") != "OK" or row.get("raw_status") == "DOWN"
        if bad:
            down_now.add("channel%s" % row.get("ch"))

    live = [c for c in chans if c.get("enabled", True) and not c.get("quarantined")]
    quarantined = [c for c in chans if c.get("quarantined")]

    # ---- systemic-outage guard --------------------------------------------
    frac = (len(down_now) / len(live)) if live else 0
    systemic = frac > SYSTEMIC_FRACTION
    if systemic:
        log("SYSTEMIC GUARD: %d/%d live channels down (%.0f%%) > %.0f%% — quarantine SUSPENDED; "
            "this looks like OUR fault (box/CDN/disk), not the provider."
            % (len(down_now), len(live), frac * 100, SYSTEMIC_FRACTION * 100))
        alert("SYSTEMIC outage — quarantine suspended",
              "%d of %d live channels are DOWN (%.0f%%).\nQuarantine is suspended so the client "
              "lineup is not emptied. Investigate the box/CDN, not the provider."
              % (len(down_now), len(live), frac * 100))

    budget = PROBE_BUDGET
    changed = False
    newly_q, restored = [], []

    # ---- 1. consider quarantining channels that have been DOWN a while ----
    for c in live:
        name = c["channel_name"]
        if name in down_now:
            streaks[name] = streaks.get(name, 0) + 1
        else:
            streaks.pop(name, None)
            continue

        if systemic or streaks.get(name, 0) < QUARANTINE_AFTER:
            continue
        if len(quarantined) + len(newly_q) >= MAX_QUARANTINED:
            log("MAX_QUARANTINED (%d) reached — not hiding %s" % (MAX_QUARANTINED, name))
            continue
        if budget <= 0:
            continue

        # Proof step: is this actually the provider's fault?
        srcs = sources_of(c)[:MAX_PROBE_SOURCES]
        best, tried = 0, 0
        for u in srcs:
            if budget <= 0:
                break
            b, ok = probe(u, acct)
            budget -= 1
            tried += 1
            best = max(best, b)
            if b >= PROBE_MIN_BYTES:
                break

        if best >= PROBE_MIN_BYTES:
            log("%s is DOWN but its source still delivers %d bytes — OUR fault, NOT quarantining. "
                "Check CPU/disk/encoder." % (name, best))
            alert("channel%s down but source is healthy" % name.replace("channel", ""),
                  "%s (%s) is DOWN for the viewer, yet its provider source delivered %d bytes in a "
                  "%ds probe. The fault is on our side (CPU starvation, encoder, disk or CDN), so it "
                  "was NOT hidden from the lineup. Investigate the box."
                  % (name, c.get("display_name", ""), best, PROBE_TIMEOUT))
            continue

        reason = ("provider-side: no source delivered >=%d bytes (%d of %d sources probed, best=%d)"
                  % (PROBE_MIN_BYTES, tried, len(sources_of(c)), best))
        log("QUARANTINE %s (%s) — %s" % (name, c.get("display_name", ""), reason))
        if not dry:
            c["quarantined"] = {"since": now(), "reason": reason,
                                "last_probe": now(), "good_probes": 0}
            stop_producer(name)
        newly_q.append(c)
        changed = True

    # ---- 2. probe quarantined channels to see if the provider is back -----
    # Round-robin by staleness so one channel can't hog the budget.
    for c in sorted(quarantined, key=lambda x: x["quarantined"].get("last_probe", "")):
        if budget <= 0:
            break
        name = c["channel_name"]
        info = c["quarantined"]
        best = 0
        for u in sources_of(c)[:MAX_PROBE_SOURCES]:
            if budget <= 0:
                break
            b, ok = probe(u, acct)
            budget -= 1
            best = max(best, b)
            if b >= PROBE_MIN_BYTES:
                break

        if not dry:
            info["last_probe"] = now()
        if best >= PROBE_MIN_BYTES:
            good = info.get("good_probes", 0) + 1
            if not dry:
                info["good_probes"] = good
            if good >= RESTORE_GOOD_PROBES:
                log("RESTORE %s (%s) — provider healthy again (%d bytes, %d consecutive good probes)"
                    % (name, c.get("display_name", ""), best, good))
                if not dry:
                    c.pop("quarantined", None)
                restored.append(c)
            else:
                log("%s probe healthy (%d bytes) — %d/%d good probes before restore"
                    % (name, best, good, RESTORE_GOOD_PROBES))
            changed = True
        else:
            if info.get("good_probes"):
                if not dry:
                    info["good_probes"] = 0   # streak broken
                changed = True
            log("%s still dead upstream (best=%d bytes)" % (name, best))

    # ---- 3. persist ------------------------------------------------------
    state["down_streaks"] = streaks
    state["updated"] = now()
    if dry:
        log("DRY RUN — no changes written. would quarantine=%d would restore=%d"
            % (len(newly_q), len(restored)))
        return 0

    save_json_atomic(STATE, state)
    if changed:
        save_json_atomic(CONFIG, cfg)
        regen_lineup()

    if newly_q or restored:
        lines = []
        if newly_q:
            lines.append("HIDDEN from the client lineup (provider not broadcasting):")
            lines += ["  - %s (%s): %s" % (c["channel_name"], c.get("display_name", ""),
                                           c["quarantined"]["reason"]) for c in newly_q]
        if restored:
            lines.append("RESTORED to the lineup (provider broadcasting again):")
            lines += ["  - %s (%s)" % (c["channel_name"], c.get("display_name", "")) for c in restored]
        alert("Lineup changed: %d hidden, %d restored" % (len(newly_q), len(restored)),
              "\n".join(lines))

    log("run done: live=%d quarantined=%d down=%d newly_hidden=%d restored=%d probes_used=%d"
        % (len(live), len(quarantined) + len(newly_q) - len(restored), len(down_now),
           len(newly_q), len(restored), PROBE_BUDGET - budget))
    return 0


if __name__ == "__main__":
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    lock = open("/tmp/quarantine_manager.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another run in progress")
        sys.exit(0)
    sys.exit(main())
