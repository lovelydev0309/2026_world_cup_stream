# Architecture

Copyright (c) 2026 lovelydev0309. All rights reserved. Proprietary — see [LICENSE](../LICENSE).

## 1. Overview

```
 IPTV provider accounts                 Origin VM (/opt/streaming-stack)                    Viewers
 ──────────────────────      ─────────────────────────────────────────────────────      ──────────────
                             streaming-rpa (systemd)
  tvon247 (MX/US/ZA)  ──┐      └─ rpa_watchdog.sh ── one run_channel.sh per channel
  one-connection lines ─┼──►        └─ ffmpeg  ──►  hls/<channel>/index.m3u8 + .ts
                        │                                   │
                        │    nginx-rtmp container  ◄────────┘   /hls/  /player/  ──► Bunny CDN ──► web players
                        │                                                                       ──► Telegram mini-apps
                        │    xtream-gateway (host-only :8091, via nginx) ─────────────────────► TiviMate / Smarters
                        │
                        └─── account jobs (cron): status · rebalance · enforce caps · stand-ins
                             lineup (cron, 1 min): player/channels.json, channels_za.json
                             monitoring: live-status, stall-monitor, synthetic decode via CDN, alerts
```

Everything runs on one GCP VM. The only container is the web origin (`nginx-rtmp`); producers,
supervisors and jobs run on the host under systemd and cron.

## 2. Components

### 2.1 Streaming (`src/streaming/`)

| File | Role |
|---|---|
| `rpa_watchdog.sh` | Supervisor (systemd `streaming-rpa`, 10 s loop). Starts `run_channel.sh` for every enabled channel, restarts dead or stale producers (90 s grace), removes duplicates. |
| `run_channel.sh` | The producer for one channel. Resolves the source, runs `ffmpeg` into `hls/<channel>/`, and owns every recovery decision for that channel (below). |
| `hls_cleanup.sh` | Removes orphaned segments left by restarts (disk-full protection). |
| `hls_video_guard.py` | Backstop: never leaves an audio-only playlist on the CDN. |
| `audio_guard.sh` | Detects playlists whose audio track disappeared. |

**Producer behaviour (`run_channel.sh`)**

- **One producer per channel**, enforced with an `flock` on `/tmp/stream_lock_<channel>`.
- **Encoding**: libx264 `ultrafast`, CRF 24, `-maxrate 1400k -bufsize 2800k` (bounded bitrate
  because there is no ABR ladder), output fps matched to the source cadence by exact-ratio
  decimation, AAC 48 kHz stereo.
- **HLS**: MPEG-TS, `hls_time 4`, `hls_list_size 40`, `append_list + omit_endlist + temp_file`
  so a restart never ends the playlist and the CDN never reads a half-written file.
- **Stall watchdog**: `ffmpeg` is killed after 14 s without write progress (per-channel override
  for bursty feeds).
- **Planned token refresh**: providers whose redirect tokens expire are reconnected every
  240 s, before the token dies.
- **Failover**: a fast failure moves to the next entry in the channel's `source_urls`; a run
  that streamed and then stalled first retries the same source. Every source is tried before
  the standby slate.
- **Backoff**: consecutive full sweeps without a healthy run back off 0 → 300 s, so one dead
  channel cannot burn connection slots needed by healthy ones.
- **Seamless 6-hour session reset**: bounds the PTS so hls.js never overflows its 32-bit
  timestamp; emitted as a single `#EXT-X-DISCONTINUITY` while keeping buffered segments, so
  viewers do not rebuffer.
- **One-connection mode** (`single_connection: true`): for lines that allow exactly one
  connection — no planned refresh, no probe connections, a 15 s gap between connections and a
  persisted 0 → 900 s backoff.
- **Line pools** (`line_pool`, `shift_secs`): one-connection lines take turns in shifts so no
  line is online 24/7; a dead on-shift line fails over to the next pool line.

### 2.2 Provider accounts (`src/accounts/`)

Provider accounts have hard connection limits; going over a limit makes the provider starve every
stream on that account. These jobs keep load within limits:

| File | Schedule | Role |
|---|---|---|
| `account_status.py` | every 10 min | Reads each account's status and `max_connections`. Expired accounts count as zero capacity. |
| `rebalance_accounts.py` | every 30 min | Reorders each channel's `source_urls` so primaries and failover hops are spread in proportion to capacity. |
| `enforce_caps.py` | every 5 min | Counts live connections and moves the excess off any over-cap account to the account with the most free slots (probe first, at most 6 moves per run, 10 min cooldown). |
| `substitute_manager.py` | every 10 min | Puts a labelled stand-in channel on air when the provider feed has been dead for 5 h; restores the original after 6 healthy probes. Never touches channels taken down on purpose. |

### 2.3 Lineup (`src/lineup/`)

| File | Role |
|---|---|
| `gen_landing_json.py` | Every minute: writes `player/channels.json` (all sites), `player/majo/channels.json` and `player/kozee/channels.json`. A channel that has been unwatchable for 180 s is left out until it recovers (suspended if more than 25 % look bad at once). Logo URLs carry `?v=<mtime>` so external sites pick up changed logos. |
| `gen_za_json.py` | Writes `player/channels_za.json`: the full South Africa list with a `status` field (not health-pruned). |
| `live_status.py` | Writes `player/live.json`, the per-channel live state the lineup and dashboards use. |

### 2.4 Monitoring (`src/monitoring/`)

| File | Role |
|---|---|
| `synthetic_monitor.py` | Decodes every channel **through the CDN**, as a viewer would; writes `player/status.json` for the status page; alerts after 2 consecutive bad runs. |
| `quality_probe.sh` | Deep check of local output: frames decoding, audio present and not silent, resolution, decode errors. |
| `stall_monitor.sh` | Viewer-visible freeze detection and digest. |
| `outage_alert.py`, `send_alert.sh` | Telegram alerts to the operations group. |
| `app_monitor.py` | Logs in to the Xtream gateway as a monitor member and checks the end-user path. |
| `healthcheck.sh` | Manual end-to-end health report. |

### 2.5 Gateway (`src/gateway/`)

- `xtream_gateway.py` — Xtream Codes compatible API (`player_api.php`, `get.php`, stream
  redirects) so members of the reseller site can log in with TiviMate / IPTV Smarters.
  Authentication is delegated to the shop's membership check; streams are served from our own
  HLS. Binds host-only and is reached through nginx.
- `admin_api.py` — internal API behind the admin pages (channel list, pause/resume, account
  balance, rebalance jobs). Token-protected; reachable only through nginx on 443.

### 2.6 VOD (`src/vod/`)

Two catalogues (Spanish `vod-disk`, English `vod-disk-us`) of self-hosted HLS movies: ingest from
the provider's VOD API (`vod_ingest2.py`), daily auto-update and watch-list, curation by
rating, blocklist enforcement, genre normalisation, subtitle fetching and AI transcription, and
catalogue publishing.

### 2.7 Telegram (`src/telegram/`)

Menu and shop bots (`kozee_bot.py`, `buy_bot.py`), menu/plan sync from the shop back-office,
and catalogue upload tools for the Telegram movie mini-apps.

## 3. Configuration and secrets

- `config/channels.json` is the single source of truth for the lineup — see
  [CONFIGURATION.md](CONFIGURATION.md).
- Sources are written as `http://@@ALIAS@@/<stream_id>`; `@@ALIAS@@` is replaced at runtime from
  the git-ignored `config/accounts.env`. No credential is ever stored in git.

## 4. Invariants

These are relied on throughout the code; breaking one has caused production incidents.

1. **One producer per HLS directory.** Two writers on one playlist freeze players.
2. **A channel publishes only its own stream** — `hls` must contain `/hls/<own name>/`.
3. **The server's playlist window outlasts the player's maximum live latency**, otherwise a
   player that drifted behind requests segments that no longer exist.
4. **Expired accounts have zero capacity** even though the provider still reports
   `max_connections` for them.
5. **Every scheduled job that writes `config/channels.json` takes its lock**
   (`/tmp/rebalance.lock`, `/tmp/enforce_caps.lock`, `/tmp/substitute.lock`), and manual edits
   take the same locks.
6. **One-connection lines never see a second connection** — no probes, no overlap, and a pool
   line is never shared between channels.

## 5. Design decisions

| Decision | Reason |
|---|---|
| MPEG-TS segments, not fMP4 | fMP4 was rolled out and reverted: playlists became unparsable under restart churn. |
| Bounded bitrate (`-maxrate 1400k`) | No ABR ladder; unbounded 540p peaked at 4–7 Mbit/s and stalled slower viewers. |
| Per-channel producers on the host | Isolation: one bad feed cannot affect another channel's process; simple to supervise. |
| Health-pruned public lineup | A channel that cannot be watched should not be offered; separate full lists exist where a site needs a fixed menu. |
| Aliases instead of credentials | The configuration can be versioned and reviewed without exposing provider accounts. |
