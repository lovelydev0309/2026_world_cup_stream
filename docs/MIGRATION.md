# Layout migration (2026-10)

Copyright (c) 2026 lovelydev0309. All rights reserved. Proprietary — see [LICENSE](../LICENSE).

The repository moved from a flat layout to `src/` (code by domain) and `deploy/` (everything the
server installs). Runtime paths that are part of the public interface did not change: `config/`,
`player/` (served at `/player/`), `hls/`, `standby/`, and the container name `nginx-rtmp`.

## Path map

| Old | New |
|---|---|
| `scripts/run_channel.sh`, `rpa_watchdog.sh`, `hls_cleanup.sh`, `hls_video_guard.py`, `audio_guard.sh` | `src/streaming/` |
| `scripts/account_status.py`, `rebalance_accounts.py`, `enforce_caps.py`, `substitute_manager.py` | `src/accounts/` |
| `scripts/gen_landing_json.py`, `gen_za_json.py`, `live_status.py` | `src/lineup/` |
| `scripts/synthetic_monitor.py`, `quality_probe.sh`, `stall_monitor.sh`, `healthcheck.sh`, `outage_alert.py`, `app_monitor.py`, `send_alert.sh` | `src/monitoring/` |
| `scripts/xtream_gateway.py`, `admin_api.py` | `src/gateway/` |
| `scripts/vod_*`, `majo_*`, `add_subs.py`, `ai_subtitles_*`, `us_subs_*`, `omdb_rerate.py`, `finalize_new_vod.py`, `vc_aggregate.py` | `src/vod/` |
| `telegram/*.py` | `src/telegram/` |
| `scripts/make_channel_logos.py`, `rollout_retry.sh` | `tools/` |
| `nginx-rtmp/` | `deploy/nginx/` |
| `systemd/*.service`, `telegram/*.service` | `deploy/systemd/` (now the units the server actually runs) |
| server crontab | `deploy/cron/crontab` |

Removed as dead code (still in git history): `scripts/watchdog.py`, `scripts/start_fallback.sh`
and the `streaming-stack`, `stream-fallback@`, `stream-watchdog@` units — the June
proof-of-concept RTMP-fallback design, never installed on the current server.

## Player pages

The 121 per-channel pages `player/channel<N>.html` were copies of one page that had drifted into
two versions (51 pages without the later playback fixes, 54 with them, plus title-only and
comment-only variants; 14 pages also carried a wrong `CHANNEL` label). They are replaced by one page,
`player/watch.html?ch=<id>`, built from the newest version, so every channel now gets the same
playback fixes. `index.html` links to it, and nginx redirects the old URLs, so existing links keep
working. The migration retires the old files only after the redirect is live.

## Code changes that came with the move

- Every script finds the project root from its own **resolved** location
  (`os.path.realpath(__file__)`, `readlink -f`), so it works from `src/<group>/` and when
  started through a compatibility link at an old path.
- Script-to-script paths (`os.path.join(PROJECT, "src", "lineup", "gen_landing_json.py")`,
  `src/streaming/run_channel.sh`, …) and imports point at the new locations.
- `admin-api.service` no longer carries its token inline; it reads `config/admin-api.env`
  (git-ignored). The migration creates that file from the current unit.
- Deploying the new tree also ships two already-committed changes that were never deployed:
  `account_status.py` / `rebalance_accounts.py` skip the one-connection `ACCT_ZA*` lines when
  polling the provider API (c734607).

## Server migration

Script: `deploy/migration/migrate_layout.sh`. Run as root on the server from an extracted copy
of the new tree.

1. **Pre-flight (no changes):** `bash deploy/migration/migrate_layout.sh` — verifies that every
   old-layout file, player page, unit and the crontab still match the snapshot in
   `deploy/migration/`. If anything changed on the server since the snapshot, it stops.
2. **Apply:** `bash deploy/migration/migrate_layout.sh --apply`
   1. backs up everything it replaces to `/root/pre-restructure-<timestamp>/`;
   2. installs `src/`, `tools/`, `deploy/`, `docs/`, `LICENSE`, `README.md`, the header versions
      of the player pages and `config/accounts.env.example`; creates `config/admin-api.env`;
   3. turns every old path into a link to its new file (running processes keep their copy);
   4. installs the units and restarts the services (the supervisor uses `KillMode=process`, so
      channels keep running), then installs the crontab;
   5. recreates the `nginx-rtmp` container for the new mount path (a few seconds);
   6. restarts the producers one at a time from `src/streaming/`.
3. **After a week of normal running**, remove the compatibility links (the command is printed
   at the end of the run).

**Rollback:** restore `code.tgz` into `/opt/streaming-stack`, `units.tgz` into
`/etc/systemd/system`, `crontab <backup>/crontab`, `systemctl daemon-reload`, restart the
services and `docker compose up -d --no-build nginx-rtmp`.
