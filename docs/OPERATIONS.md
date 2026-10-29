# Operations

Copyright (c) 2026 lovelydev0309. All rights reserved. Proprietary — see [LICENSE](../LICENSE).

Production runs on one GCP VM under `/opt/streaming-stack` (not a git checkout). Access is SSH as
`root` with the operator key. Always resolve the current address first:
`dig +short live3.mzolotv.com` — the CDN pulls from that name.

## Services and schedules

| What | How it runs | Unit / schedule |
|---|---|---|
| Channel supervisor + producers | systemd | `streaming-rpa` → `src/streaming/rpa_watchdog.sh` |
| Live state | systemd | `live-status` |
| Freeze detection | systemd | `stall-monitor` |
| Xtream gateway | systemd | `xtream-gateway` |
| Admin API | systemd | `admin-api` (env: `config/admin-api.env`) |
| Telegram bots | systemd | `kozee-bot`, `majo-bot`, `kozee-buy-bot`, `majo-buy-bot` |
| Web origin | Docker | `docker compose up -d` → container `nginx-rtmp` |
| Lineup, accounts, monitoring, VOD | cron | `deploy/cron/crontab` |

## Deploying a change

1. **Compare first.** The server copy must equal the repository copy you changed
   (`md5sum`). If it differs, the server has a change git does not — reconcile before deploying.
2. **Install atomically, keep permissions:** copy to `<file>.new`, `chmod --reference` /
   `chown --reference` from the live file, then `mv` over it. Never edit a running shell script
   in place — bash reads scripts incrementally and a running producer would execute a mix of
   old and new text.
3. **Check `crontab -l`** before deploying any script that writes configuration: several jobs
   apply changes on a schedule, so deploying is applying.
4. **Restart only what needs it.** `run_channel.sh` changes take effect when a producer restarts;
   restart channels one at a time (kill the wrapper; the supervisor relaunches it within ~10 s).
5. **Verify through the CDN** (`/player/channels.json`, the status page), not only on the server.

`config/channels.json` on the server is authoritative. After any change there, copy it back to
the repository (server → repo, never the reverse).

## Routine tasks

| Task | How |
|---|---|
| Pause / resume a channel | Admin page, or set `enabled` (and `hidden` for a takedown) under the three config locks, then stop the producer |
| Add or swap a channel | Probe the stream on an account with a free slot (picture **and** audio), then edit `display_name`, `source_urls`, logo and page; restart that producer |
| Check account load | `python3 src/accounts/account_status.py` or the accounts admin page |
| Fix an over-cap account | `python3 src/accounts/enforce_caps.py --dry-run`, then `--apply` (also runs every 5 min) |
| Regenerate the lineup | `python3 src/lineup/gen_landing_json.py` (runs every minute) |

## Troubleshooting

| Symptom | First checks |
|---|---|
| Many channels flapping | `df -h` (orphaned segments can fill the disk); account occupancy vs caps; expired accounts |
| One channel on the standby slate | its log in `logs/<channel>.log`; probe the source with `curl` on an account with a free slot; `401` = account disabled/expired, `403` = provider refusing, reset = provider dropping |
| Video but no audio | the source has no audio track → `force_silent_audio: true` |
| Plays a while, then stops | provider over its connection limit, or the player drifted outside the playlist window |
| Everything returns 504 on the CDN | origin DNS (`live3.mzolotv.com`) no longer points at the server |
| A website shows old names/logos | it caches its own copy; logo URLs are versioned, so a re-import fixes it |

## Logs

| Location | Contents |
|---|---|
| `logs/<channel>.log` | producer decisions (start, failover, backoff, planned restarts) |
| `logs/<channel>_stdout.log` | the same plus raw `ffmpeg` errors |
| `logs/enforce_caps.log`, `logs/rebalance.log`, `logs/substitute.log` | account jobs |
| `player/status.json` | latest synthetic decode result per channel |
