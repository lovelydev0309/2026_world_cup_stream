#!/usr/bin/env bash
# send_alert.sh "<subject>" "<body>" — email an alert via SMTP, with a per-subject
# cooldown so repeated issues don't spam the inbox. Credentials live in
# config/alert.env (NOT in git). Silently no-ops if alert.env is absent/incomplete,
# so the monitors keep running whether or not email is configured.
set -uo pipefail
CFG="/opt/streaming-stack/config/alert.env"
[ -f "$CFG" ] || exit 0
# shellcheck disable=SC1090
source "$CFG"
[ -n "${SMTP_USER:-}" ] && [ -n "${SMTP_PASS:-}" ] && [ -n "${ALERT_TO:-}" ] || exit 0

subject="${1:-Stream alert}"; body="${2:-}"; status="${3:-error}"
# status icon shown in the email content: green check = no error, red X = error
if [ "$status" = "ok" ]; then emoji="🟢✔"; else emoji="🔴❌"; fi
COOLDOWN=${ALERT_COOLDOWN:-900}      # min seconds between identical-subject alerts
HOST=${SMTP_HOST:-smtp.gmail.com}; PORT=${SMTP_PORT:-465}

mkdir -p /tmp/alert_state
# Dedup key = alert TYPE (first word) + channel id, NOT the full subject. The monitors put
# variable content in the subject (the freeze gap "(52s)", the quality "-flags"), which made
# every subject unique → the md5 cooldown never matched → an email per event (the "continuous
# error email" flood). Keying on type+channel makes the cooldown actually suppress repeats.
_prefix=$(printf '%s' "$subject" | awk '{print $1}')
_chan=$(printf '%s' "$subject" | grep -oE 'channel[0-9]+' | head -1)
key=$(printf '%s_%s' "$_prefix" "${_chan:-$subject}" | md5sum | cut -c1-16)
stamp="/tmp/alert_state/$key"
now=$(date +%s)
if [ -f "$stamp" ] && [ $(( now - $(cat "$stamp" 2>/dev/null || echo 0) )) -lt "$COOLDOWN" ]; then
  exit 0
fi
echo "$now" > "$stamp"

tmp=$(mktemp)
{
  echo "From: Stream Monitor <$SMTP_USER>"
  echo "To: $ALERT_TO"
  echo "Subject: [live3] $emoji $subject"
  echo "Content-Type: text/plain; charset=UTF-8"
  echo "Date: $(date -R)"
  echo
  printf '%s  %s\n' "$emoji" "$body"
  echo
  echo "-- automated alert from the stream-quality monitor (live3.mzolotv.com / 15 channels)"
} > "$tmp"

# port 465 = implicit SSL (Gmail); 587 = STARTTLS (Outlook/Office365)
scheme="smtps"; [ "$PORT" = "587" ] && scheme="smtp"
err=$(curl -s --url "$scheme://$HOST:$PORT" --ssl-reqd \
     --user "$SMTP_USER:$SMTP_PASS" \
     --mail-from "$SMTP_USER" --mail-rcpt "$ALERT_TO" \
     --upload-file "$tmp" 2>&1)
rc=$?
[ "$rc" -ne 0 ] && echo "[$(date -u +%FT%TZ)] alert send FAILED rc=$rc: $err" >> /opt/streaming-stack/logs/alert_send.log
rm -f "$tmp"
exit "$rc"
