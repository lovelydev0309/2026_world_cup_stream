# Streaming Stack

24/7 live-TV re-streaming platform: it pulls channels from IPTV provider accounts, re-encodes
them to a uniform HLS output, publishes them through a CDN, and serves them to the MzoloTV /
MajoTV / KozeeTV web players, Telegram mini-apps and an Xtream-compatible gateway for
TiviMate / IPTV Smarters. A movie (VOD) catalogue is served from the same origin.

> **Proprietary software — all rights reserved.** See [LICENSE](LICENSE). No permission is
> granted to copy, modify, run or develop this code without the copyright holder's written
> permission.

> **Compliance notice.** The system supports authorised and licensed video sources only.
> DRM bypass, key extraction and capture-protection circumvention are out of scope and not
> implemented.

## At a glance

| | |
|---|---|
| **Channels** | ~60 live channels (Mexico, US, South Africa) from several provider account families |
| **Per channel** | one supervised `ffmpeg` producer → HLS (MPEG-TS, 4 s segments, 540p/720p H.264 + AAC 48 kHz) |
| **Origin** | GCP VM, `nginx-rtmp` container serving `/hls/` and `/player/` on 80/443/8080 |
| **Delivery** | Bunny CDN in front of the origin (`stream.tv247on.com`) |
| **Self-healing** | stall watchdog, account failover, connection-cap enforcement, backoff, standby slate |
| **Monitoring** | end-to-end decode probe via the CDN, status dashboard, Telegram outage alerts |

## Repository layout

```
.
├── LICENSE                 proprietary licence (all rights reserved)
├── README.md
├── docker-compose.yml      origin web server (nginx-rtmp container)
├── docs/                   architecture, configuration, operations, migration
├── config/                 channel lineup + bot menus (no secrets; see accounts.env.example)
├── deploy/
│   ├── nginx/              Dockerfile + nginx.conf for the origin container
│   ├── systemd/            production service units
│   └── cron/crontab        production scheduled jobs
├── src/
│   ├── streaming/          channel producer (run_channel.sh), supervisor, HLS housekeeping
│   ├── accounts/           provider-account capacity: status, rebalancing, cap enforcement, stand-ins
│   ├── lineup/             published lineup JSON (channels.json / channels_za.json), live state
│   ├── monitoring/         synthetic decode monitor, quality probe, stall monitor, alerts
│   ├── gateway/            Xtream-compatible gateway + internal admin API
│   ├── vod/                movie catalogue ingest, curation, subtitles, publishing
│   └── telegram/           Telegram menu/shop bots and catalogue sync
├── tools/                  one-off maintenance utilities
├── player/                 static web root served at /player/ (players, admin pages, logos)
└── standby/                standby slate video shown while a source is unavailable
```

Runtime directories (`hls/`, `logs/`, `cache/`, movie disks) exist only on the server and are
not versioned.

## How it works (short)

1. **`src/streaming/rpa_watchdog.sh`** (systemd `streaming-rpa`) keeps one
   **`run_channel.sh`** running per enabled channel in `config/channels.json`.
2. `run_channel.sh` resolves the channel's provider account from `config/accounts.env`,
   runs `ffmpeg` into `hls/<channel>/`, and handles stalls, token refresh, failover across
   the channel's `source_urls`, backoff and the standby slate.
3. Account jobs (`src/accounts/`) keep every provider account within its connection limit.
4. `src/lineup/gen_landing_json.py` publishes the healthy lineup every minute as
   `player/channels.json`, which the websites, apps and the gateway read.
5. `src/monitoring/` decodes every channel through the CDN and alerts on real outages.

Full detail: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, data flow, failure handling, design decisions |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | `channels.json` fields, `accounts.env` keys, admin/env files |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | deployment, routine tasks, troubleshooting runbook |
| [docs/MIGRATION.md](docs/MIGRATION.md) | old → new path map and the server migration procedure |

## Secrets

Provider credentials, bot tokens and API keys never enter git. They live in the server-only
files `config/accounts.env` and `config/admin-api.env` (both git-ignored); the template is
[config/accounts.env.example](config/accounts.env.example). Channel sources reference accounts
by alias (`http://@@ACCT_LAT1@@/108880`) and are resolved at runtime.
