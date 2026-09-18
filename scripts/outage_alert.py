#!/usr/bin/env python3
"""Post to the OPS Telegram group when a channel has been down for 30+ minutes, and again
when it comes back.

Client request (2026-09-18): "configure a Telegram bot that sends alerts to the group
whenever a channel remains offline for more than 30 minutes ... timely notifications in
the event of a large-scale channel outage."

Source of truth is player/live.json, written every 5s by live_status.py from the box's own
HLS output. A channel counts as DOWN when its state is anything but LIVE -- SLATE (holding
card), SLOW, STALLED or OFF are all things a viewer would complain about.

Designed so the group is informed, not spammed:

  * ONE alert per outage. A channel that stays down for six hours produces one "down"
    message and one "recovered" message, not one every run.
  * Channels that cross the threshold in the same run are BATCHED into a single message.
    A provider-wide failure taking out 20 channels at once is one post listing all 20,
    headed as a large-scale outage -- which is the case the client specifically asked to
    be told about promptly.
  * 30 minutes CONTINUOUS. Producers reconnect on every provider token expiry (~90s) and
    read STALLED for 10-20s each time; that must never page anyone. The clock resets on
    the first healthy reading.
  * Recovery is reported the moment the channel is LIVE again, with how long it was out.
  * If live.json itself is stale (monitor stopped), that is reported ONCE as its own
    alert, and no channel alerts are raised from stale data -- a dead monitor must not
    look like 76 dead channels.

Credentials: TG_OUTAGE_BOT_TOKEN and TG_OUTAGE_CHAT_ID in the gitignored config/accounts.env.
State: cache/outage_alert_state.json. Cron every minute.
"""
import json, os, time, urllib.request, urllib.parse, urllib.error

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVE = os.path.join(PROJECT, "player", "live.json")
STATE = os.path.join(PROJECT, "cache", "outage_alert_state.json")
ACCOUNTS = os.path.join(PROJECT, "config", "accounts.env")
LOG = os.path.join(PROJECT, "logs", "outage_alert.log")

DOWN_AFTER = 30 * 60        # seconds continuously not-LIVE before alerting
LIVE_MAX_AGE = 120          # live.json older than this = monitor is down, not the channels
LARGE_SCALE = 5             # this many channels in one batch => headed as a large-scale outage
HEALTHY = ("LIVE",)


def log(msg):
    line = "[%s] %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        open(LOG, "a").write(line + "\n")
    except OSError:
        pass


def env():
    out = {}
    try:
        for line in open(ACCOUNTS):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def send(text):
    e = env()
    tok, chat = e.get("TG_OUTAGE_BOT_TOKEN"), e.get("TG_OUTAGE_CHAT_ID")
    if not tok or not chat:
        log("no TG_OUTAGE_BOT_TOKEN / TG_OUTAGE_CHAT_ID in accounts.env -- not sending")
        return False
    body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        r = json.load(urllib.request.urlopen(
            urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % tok, data=body),
            timeout=20))
        return bool(r.get("ok"))
    except urllib.error.HTTPError as ex:
        log("telegram %s: %s" % (ex.code, ex.read().decode()[:160]))
    except Exception as ex:
        log("telegram error: %s" % str(ex)[:120])
    return False


def human(sec):
    m = int(sec // 60)
    return "%dh %02dm" % (m // 60, m % 60) if m >= 60 else "%dm" % m


def load_state():
    try:
        return json.load(open(STATE))
    except Exception:
        return {"channels": {}, "monitor_stale_alerted": False}


def save_state(st):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        json.dump(st, open(tmp, "w"), indent=1)
        os.replace(tmp, STATE)
    except OSError:
        pass


def main():
    now = time.time()
    st = load_state()
    chans = st.setdefault("channels", {})

    # --- is the monitor itself alive? ---------------------------------------------------
    try:
        live = json.load(open(LIVE))
        age = now - time.mktime(time.strptime(live["updated"], "%Y-%m-%dT%H:%M:%SZ"))
    except Exception:
        live, age = None, 10 ** 9
    if live is None or age > LIVE_MAX_AGE:
        if not st.get("monitor_stale_alerted"):
            send("⚠️ <b>Monitor is not updating</b>\n"
                 "live.json is %s old, so channel health is unknown right now. "
                 "No channel alerts will be raised until it recovers." %
                 ("missing" if live is None else human(age)))
            st["monitor_stale_alerted"] = True
            log("monitor stale (%.0fs) -- alerted" % age)
        save_state(st)
        return
    if st.get("monitor_stale_alerted"):
        send("✅ <b>Monitor is updating again.</b>")
        st["monitor_stale_alerted"] = False

    # --- per-channel clocks ------------------------------------------------------------
    newly_down, recovered = [], []
    seen = set()
    for c in live.get("channels", []):
        name = c["name"]
        seen.add(name)
        rec = chans.setdefault(name, {"down_since": None, "alerted": False})
        rec["title"] = c.get("title", name)
        rec["country"] = c.get("country", "")
        if c.get("state") in HEALTHY:
            if rec.get("alerted"):
                # keep `alerted` set until the recovery message actually goes out
                recovered.append((name, rec["title"], rec["country"], now - rec["down_since"]))
            else:
                rec["down_since"] = None
            rec["state"] = "LIVE"
            continue
        rec["state"] = c.get("state")
        if rec["down_since"] is None:
            rec["down_since"] = now
        elif not rec["alerted"] and now - rec["down_since"] >= DOWN_AFTER:
            # not marked yet -- only after Telegram accepts the message, so that a failed
            # send (bot not in the group yet, API blip) is retried next minute, not lost
            newly_down.append((name, rec["title"], rec["country"], rec["state"], now - rec["down_since"]))

    # a channel that disappears from live.json (disabled/hidden) should not linger as "down"
    for name in [n for n in chans if n not in seen]:
        chans.pop(name, None)

    # --- send ---------------------------------------------------------------------------
    if newly_down:
        n = len(newly_down)
        still = sum(1 for r in chans.values() if r.get("alerted"))
        head = ("🚨 <b>LARGE-SCALE OUTAGE — %d channels down 30+ min</b>" % n if n >= LARGE_SCALE
                else "🔴 <b>%s down for 30+ minutes</b>" % ("Channel" if n == 1 else "%d channels" % n))
        lines = ["• <b>%s</b> (%s) — %s, %s" % (t, co, s, human(d)) for _, t, co, s, d in
                 sorted(newly_down, key=lambda x: (x[2], x[1]))]
        tail = "\n\n%d channel%s currently down in total." % (still + n, "" if still + n == 1 else "s") \
               if still else ""
        if send(head + "\n\n" + "\n".join(lines) + tail):
            for nm, _, _, _, _ in newly_down:
                chans[nm]["alerted"] = True
            log("alerted: %d down (%s)" % (n, ", ".join(t for _, t, _, _, _ in newly_down)))
        else:
            log("send failed; %d outage(s) will retry next run" % n)

    if recovered:
        n = len(recovered)
        head = "🟢 <b>%s back</b>" % ("Channel" if n == 1 else "%d channels" % n)
        lines = ["• <b>%s</b> (%s) — was out %s" % (t, co, human(d)) for _, t, co, d in
                 sorted(recovered, key=lambda x: (x[2], x[1]))]
        if send(head + "\n\n" + "\n".join(lines)):
            for nm, _, _, _ in recovered:
                chans[nm]["alerted"] = False
                chans[nm]["down_since"] = None
            log("recovered: %d (%s)" % (n, ", ".join(t for _, t, _, _ in recovered)))
        else:
            log("recovery send failed; will retry next run")

    save_state(st)


if __name__ == "__main__":
    main()
