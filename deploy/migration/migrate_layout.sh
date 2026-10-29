#!/usr/bin/env bash
# Copyright (c) 2026 lovelydev0309. All rights reserved.
# Proprietary and confidential. See LICENSE. Unauthorized copying, modification or use is prohibited.
#
# migrate_layout.sh — move the production server from the old flat layout (scripts/, telegram/,
# nginx-rtmp/, systemd/) to the src/ + deploy/ layout, without taking channels off air.
#
#   bash deploy/migration/migrate_layout.sh            # pre-flight only: checks, changes nothing
#   bash deploy/migration/migrate_layout.sh --apply    # migrate
#
# Run as root ON THE SERVER from an extracted copy of the new tree (the "stage"), e.g.
#   mkdir /root/stage && tar -C /root/stage -xzf restructure.tar.gz
#   bash /root/stage/deploy/migration/migrate_layout.sh
#
# Safety model
#   * Pre-flight refuses to run if any old-layout file, the crontab or a unit on the server differs
#     from the snapshot this migration was built from (base.md5 / crontab.base / units.md5).
#   * Old paths become symlinks to the new files, so anything still calling an old path keeps
#     working (code resolves its project root through the link). Running producers keep running.
#   * systemd's streaming-rpa uses KillMode=process: restarting the supervisor never kills channels.
#   * Producers move to the new path one at a time (STAGGER seconds apart); the players' buffer
#     covers each ~15 s reconnect.
#   * Everything replaced is backed up to /root/pre-restructure-<timestamp>/ first.
set -euo pipefail

P=/opt/streaming-stack
STAGE="$(cd "$(dirname "$(readlink -f "$0")")/../.." && pwd)"
M="$STAGE/deploy/migration"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1
STAGGER=20
TS=$(date -u +%Y%m%d-%H%M%S)
BK=/root/pre-restructure-$TS
UNITS="admin-api kozee-bot kozee-buy-bot live-status majo-bot majo-buy-bot stall-monitor streaming-rpa xtream-gateway"

say()  { echo "[$(date -u +%H:%M:%S)] $*"; }
die()  { echo "ABORT: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
[ -d "$P/config" ] && [ -f "$P/config/channels.json" ] || die "$P does not look like the production tree"
[ -f "$STAGE/src/streaming/run_channel.sh" ] || die "stage $STAGE is not the new tree"
[ "$STAGE" != "$P" ] || die "run from an extracted stage copy, not from $P"

# ── 1. Pre-flight: the server must still be exactly what this migration was built from ─────────
say "pre-flight: comparing $P with the migration snapshot"
bad=0
while read -r sum old new; do
    [ -e "$P/$old" ] || { echo "  missing on server: $old"; bad=1; continue; }
    [ "$(md5sum < "$P/$old" | cut -d' ' -f1)" = "$sum" ] || { echo "  changed on server since snapshot: $old"; bad=1; }
done < <(grep -v '^#' "$M/base.md5")
crontab -l 2>/dev/null | grep -v '^# \[rolled-back' | diff -q - "$M/crontab.base" >/dev/null \
    || { echo "  crontab changed since snapshot"; bad=1; }
while read -r sum unit; do
    [ "$(md5sum < "/etc/systemd/system/$unit" | cut -d' ' -f1)" = "$sum" ] || { echo "  unit changed since snapshot: $unit"; bad=1; }
done < "$M/units.md5"
while read -r sum page; do
    [ "$(md5sum < "$P/$page" 2>/dev/null | cut -d' ' -f1)" = "$sum" ] || { echo "  page changed since snapshot: $page"; bad=1; }
done < <(grep -v '^#' "$M/pages.md5")
[ $bad = 0 ] || die "server differs from the snapshot; rebuild the migration from the current server state"
say "pre-flight OK: $(grep -vc '^#' "$M/base.md5") files, $(grep -vc '^#' "$M/pages.md5") pages, crontab and $(wc -l < "$M/units.md5") units match"
if [ $APPLY = 0 ]; then say "dry run: nothing changed (pass --apply to migrate)"; exit 0; fi

# ── 2. Backup ───────────────────────────────────────────────────────────────────────────────────
say "backup -> $BK"
mkdir -p "$BK"
tar -C "$P" -czf "$BK/code.tgz" scripts telegram nginx-rtmp systemd tools docker-compose.yml README.md $(grep -v '^#' "$M/pages.md5" | awk '{print $2}')
crontab -l > "$BK/crontab"
tar -C /etc/systemd/system -czf "$BK/units.tgz" $(for u in $UNITS; do echo "$u.service"; done) streaming-rpa.service.d

# ── 3. New tree ─────────────────────────────────────────────────────────────────────────────────
say "install src/ tools/ deploy/ docs/ LICENSE README.md config/accounts.env.example"
for d in src tools deploy docs; do mkdir -p "$P/$d"; cp -a "$STAGE/$d/." "$P/$d/"; done
cp -a "$STAGE/LICENSE" "$STAGE/README.md" "$P/"
cp -a "$STAGE/config/accounts.env.example" "$P/config/"
if [ ! -f "$P/config/admin-api.env" ]; then           # move the inline token out of the unit
    grep -E '^Environment=ADMIN_' /etc/systemd/system/admin-api.service | sed 's/^Environment=//' > "$P/config/admin-api.env"
    chmod 600 "$P/config/admin-api.env"
    say "config/admin-api.env created ($(wc -l < "$P/config/admin-api.env") keys)"
fi

say "player pages: install the new versions (per-channel pages are retired after step 6)"
while read -r sum page; do
    [ -f "$STAGE/$page" ] || continue
    cp "$STAGE/$page" "$P/$page.new" && chmod --reference="$P/$page" "$P/$page.new" \
        && chown --reference="$P/$page" "$P/$page.new" && mv "$P/$page.new" "$P/$page"
done < <(grep -v '^#' "$M/pages.md5")
cp "$STAGE/player/watch.html" "$P/player/watch.html.new" && chmod --reference="$P/player/index.html" "$P/player/watch.html.new" \
    && chown --reference="$P/player/index.html" "$P/player/watch.html.new" && mv "$P/player/watch.html.new" "$P/player/watch.html"

# ── 4. Old paths -> symlinks to the new files (running processes keep their open copy) ──────────
say "old paths become links to the new files"
while read -r sum old new; do
    [ -f "$P/$new" ] || die "new file missing: $new"
    ln -sfn "$P/$new" "$P/$old.migrating" && mv -T "$P/$old.migrating" "$P/$old"
done < <(grep -v '^#' "$M/base.md5" | awk '$3 != "-"')

# ── 5. systemd units + crontab ──────────────────────────────────────────────────────────────────
say "install systemd units"
for u in $UNITS; do install -m 644 "$P/deploy/systemd/$u.service" "/etc/systemd/system/$u.service"; done
install -m 644 "$P/deploy/systemd/streaming-rpa.service.d/killmode.conf" /etc/systemd/system/streaming-rpa.service.d/killmode.conf
systemctl daemon-reload
for u in $UNITS; do
    systemctl is-active --quiet "$u" || { say "  $u was not active - leaving it stopped"; continue; }
    systemctl restart "$u"; sleep 2
    systemctl is-active --quiet "$u" && say "  $u restarted" || die "$u failed to restart - restore from $BK"
done
say "install crontab"
crontab "$P/deploy/cron/crontab"

# ── 6. Origin container: build context and nginx.conf mount moved to deploy/nginx ────────────────
say "recreate nginx-rtmp with the new mount (a few seconds; players' buffers cover it)"
cp -a "$STAGE/docker-compose.yml" "$P/docker-compose.yml"
(cd "$P" && docker compose up -d --no-build nginx-rtmp)
sleep 5
curl -fsS -o /dev/null "http://127.0.0.1:8080/player/channels.json" || die "origin not answering after recreate - restore from $BK"
loc=$(curl -s -o /dev/null -w '%{redirect_url}' "http://127.0.0.1:8080/player/channel1.html")
case "$loc" in */player/watch.html?ch=channel1) say "per-channel URLs redirect to watch.html";;
    *) die "per-channel redirect not active (got '$loc') - restore from $BK";; esac
retired=0
while read -r sum page; do                     # pages the new tree no longer has (backed up in code.tgz)
    [ -f "$STAGE/$page" ] || { rm -f "$P/$page"; retired=$((retired+1)); }
done < <(grep -v '^#' "$M/pages.md5")
say "retired $retired per-channel pages"

# ── 7. Move producers to the new path, one at a time ────────────────────────────────────────────
mapfile -t CHANNELS < <(python3 -c "
import json
for c in json.load(open('$P/config/channels.json'))['channels']:
    if c.get('enabled', True): print(c['channel_name'])")
say "restarting ${#CHANNELS[@]} producers from src/streaming/ (${STAGGER}s apart)"
for ch in "${CHANNELS[@]}"; do
    pids=$(pgrep -f "run_channel\.sh ${ch}\$" || true)
    if [ -n "$pids" ] && ! tr '\0' ' ' < "/proc/$(echo "$pids" | head -1)/cmdline" | grep -q "/src/streaming/"; then
        kill -9 $pids 2>/dev/null || true
        pkill -9 -f "hls/${ch}/[0-9%]" 2>/dev/null || true
        sleep "$STAGGER"
        now=$(pgrep -f "run_channel\.sh ${ch}\$" | head -1 || true)
        if [ -n "$now" ] && tr '\0' ' ' < "/proc/$now/cmdline" | grep -q "/src/streaming/"; then say "  $ch on new path"
        else say "  WARN $ch not yet relaunched from src/streaming (supervisor will retry)"; fi
    fi
done

# ── 8. Report ───────────────────────────────────────────────────────────────────────────────────
say "done. Backup: $BK. Old paths are links for now; after a week of normal running remove them with:"
echo "  cd $P && rm -f \$(grep -v '^#' deploy/migration/base.md5 | awk '{print \$2}') && rmdir scripts telegram nginx-rtmp systemd 2>/dev/null"
