# Configuration

Copyright (c) 2026 lovelydev0309. All rights reserved. Proprietary — see [LICENSE](../LICENSE).

All configuration lives in `config/`. Only files without secrets are versioned.

| File | Versioned | Purpose |
|---|---|---|
| `channels.json` | yes | channel lineup and per-channel behaviour (this page) |
| `accounts.env` | **no** | provider accounts, bot tokens, API keys — template: `accounts.env.example` |
| `admin-api.env` | **no** | `ADMIN_BIND`, `ADMIN_PORT`, `ADMIN_TOKEN` for `admin-api.service` |
| `tg_menu*.json`, `tg_faq*.json`, `tg_plans_*.json` | yes | Telegram bot menus, FAQ and plans per shop |
| `vod_blocklist.txt` | yes | movies that must never appear in a published catalogue |

The production copy of `channels.json` on the server is authoritative: the account jobs rewrite
`source_urls` there. Sync direction is always **server → repository**.

## channels.json

```json
{ "channels": [ { "channel_name": "channel118", "display_name": "SuperSport Action HD", ... } ] }
```

The top-level `server`, `watchdog` and `iptv` sections are legacy; only `healthcheck.sh` reads
`server`. Keys starting with `_` are human comments and are ignored by the code.

### Identity and publishing

| Field | Default | Meaning |
|---|---|---|
| `channel_name` | — | Unique id, `channel<N>`; also the HLS directory name. |
| `display_name` | `channel_name` | Name shown to viewers. |
| `country` | from number | Lineup section (`Mexico`, `US`, `South Africa`, …); also selects the gateway package. |
| `logo` | `/player/logos/<name>.png` | Logo path; `<name>_light.png` is the light-theme variant. |
| `hls_url` | derived | Public playlist URL. Must contain `/hls/<channel_name>/`; a mismatch is ignored. |
| `enabled` | `true` | `false` stops the producer entirely. |
| `hidden` | `false` | Keeps the producer running but leaves the channel out of the public lineup. A deliberate takedown is `enabled: false` **and** `hidden: true`. |

### Sources

| Field | Default | Meaning |
|---|---|---|
| `source_urls` | — | Ordered list `http://@@ALIAS@@/<stream_id>`. Entry 0 is the primary; the rest are failover hops. Rewritten by the account jobs. |
| `source_url` | — | Legacy single source; ignored when `source_urls` is present. |
| `token_refresh_secs` | `240` | Planned reconnect interval for providers with expiring tokens; `0` disables it. |
| `single_connection` | `false` | One-connection line mode (no refresh, no probes, 15 s gap, long persisted backoff). |
| `line_pool` | — | Aliases that take turns serving the channel; `source_urls` must name the first one. |
| `shift_secs` | `21600` | Shift length for `line_pool` (minimum 60). |
| `stale_kill_secs` | `14` | Seconds without write progress before `ffmpeg` is restarted (allowed 8–60). |
| `substitute` | — | `{ "source_urls": [...], "label": "..." }` stand-in used by `substitute_manager.py` after a long provider outage. |

### Output

| Field | Default | Meaning |
|---|---|---|
| `video_mode` | `encode` | `encode` (re-encode, recommended) or `copy`. |
| `encode_resolution` | `960x540` | Output size in `encode` mode. |
| `bitrate`, `fps` | `2500`, `30` | Legacy hints; output fps follows the source cadence. |
| `audio_bitrate` | `128` | AAC bitrate (kbit/s). |
| `audio_sync` | `regen` | `preserve` keeps source timestamps; `regen` regenerates them. |
| `force_silent_audio` | `false` | Replace the source audio with silence (feeds with no or broken audio). |
| `use_standby` | `true` | Show the standby slate while no source works (disable for `copy` mode). |
| `standby_file` | `standby/standby.mp4` | Slate video, relative to the project root. |
| `segment_type` | `mpegts` | `fmp4` exists but is not used in production (see ARCHITECTURE §5). |

### Written by automation

`_original_source_urls` and `substituted` are maintained by `substitute_manager.py` while a
stand-in is on air. Do not edit them by hand.

## accounts.env

`KEY=value` lines, never committed. Provider accounts are `ALIAS=host:port/username/password`
and are referenced from `source_urls` as `@@ALIAS@@`. Families in use:

| Prefix | Provider / use |
|---|---|
| `ACCT<N>`, `ACCT_LAT<N>`, `ACCT_PE<N>` | tvon247 accounts (Latin template; LAT1/2/4–7 switched to the South Africa template) |
| `KOZEE<N>` | tvon247 accounts, US template |
| `ACCT_ZA<N>`, `ACCT_ZA_TEST` | South Africa seller one-connection lines (excluded from API polling) |
| `TG_*`, `MAJO_*`, `KOZEE_BUY_BOT_TOKEN`, `NOCO_*` | Telegram bots and shop back-office |
| `VOD_*`, `OMDB_API_KEY` | VOD ingest and ratings |
| `XTREAM_*` | gateway public host, signing secret, monitor member |

See `config/accounts.env.example` for every key.
