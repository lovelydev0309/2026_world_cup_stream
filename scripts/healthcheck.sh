#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# healthcheck.sh – Quick CLI health check for all channels
#
# Usage:
#   ./healthcheck.sh              # check all enabled channels
#   ./healthcheck.sh channel1     # check one channel
# ─────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
CONFIG="$PROJECT_DIR/config/channels.json"

# Terminal colours
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'; BOLD='\033[1m'

STALE_THRESHOLD=20  # seconds before an HLS stream is "stale"
TARGET="${1:-}"

# ── System resources ─────────────────────────────────────────
print_system() {
    echo -e "\n${BOLD}── System Resources ─────────────────────────────────────────${NC}"
    # CPU (1-second sample)
    CPU=$(top -bn1 | grep "Cpu(s)" | awk '{print $2 + $4}' 2>/dev/null || echo "N/A")
    # RAM
    read -r TOTAL USED FREE <<< "$(free -m | awk '/^Mem:/{print $2, $3, $4}')"
    # Disk (HLS dir)
    HLS_DIR="$PROJECT_DIR/hls"
    DISK=$(df -h "$HLS_DIR" 2>/dev/null | awk 'NR==2{print $3"/"$2" ("$5")"}' || echo "N/A")
    # Network (rough bandwidth from /proc/net/dev – 1s sample)
    IF=$(ip route | awk '/default/{print $5; exit}')
    if [[ -n "$IF" ]]; then
        RX1=$(cat /proc/net/dev | awk -v i="$IF:" '$1==i{print $2}')
        TX1=$(cat /proc/net/dev | awk -v i="$IF:" '$1==i{print $10}')
        sleep 1
        RX2=$(cat /proc/net/dev | awk -v i="$IF:" '$1==i{print $2}')
        TX2=$(cat /proc/net/dev | awk -v i="$IF:" '$1==i{print $10}')
        RX_MBPS=$(echo "scale=2; ($RX2 - $RX1) * 8 / 1048576" | bc 2>/dev/null || echo "N/A")
        TX_MBPS=$(echo "scale=2; ($TX2 - $TX1) * 8 / 1048576" | bc 2>/dev/null || echo "N/A")
        NET="${RX_MBPS} Mbps ↓ / ${TX_MBPS} Mbps ↑ (${IF})"
    else
        NET="N/A"
    fi

    printf "  %-14s %s%%\n"  "CPU:"     "$CPU"
    printf "  %-14s %s MB / %s MB used\n" "RAM:"  "$USED" "$TOTAL"
    printf "  %-14s %s\n"    "Disk(hls):" "$DISK"
    printf "  %-14s %s\n"    "Network:"   "$NET"
}

# ── Check one HLS URL ─────────────────────────────────────────
check_hls() {
    local url="$1"
    local body
    body=$(curl -sf --max-time 5 "$url" 2>/dev/null) || { echo "FAIL"; return 1; }

    # Find last .ts segment line
    local last_seg
    last_seg=$(echo "$body" | grep '\.ts' | tail -1)
    [[ -z "$last_seg" ]] && { echo "NO_SEGMENTS"; return 1; }

    local base="${url%/*}"
    local seg_url
    if [[ "$last_seg" == http* ]]; then
        seg_url="$last_seg"
    else
        seg_url="${base}/${last_seg}"
    fi

    # Check Last-Modified of segment
    local last_mod
    last_mod=$(curl -sI --max-time 4 "$seg_url" 2>/dev/null | grep -i "Last-Modified:" | cut -d' ' -f2-)
    if [[ -n "$last_mod" ]]; then
        local ts
        ts=$(date -d "$last_mod" +%s 2>/dev/null || date -j -f "%a, %d %b %Y %T %Z" "$last_mod" +%s 2>/dev/null || echo 0)
        local age=$(( $(date +%s) - ts ))
        if [[ $age -lt $STALE_THRESHOLD ]]; then
            echo "FRESH (age ${age}s)"
            return 0
        else
            echo "STALE (age ${age}s)"
            return 1
        fi
    fi
    echo "OK (no age)"
    return 0
}

# ── Check RTMP via nginx stat ─────────────────────────────────
check_rtmp() {
    local stat_url="$1"
    local key="$2"
    local xml
    xml=$(curl -sf --max-time 4 "$stat_url" 2>/dev/null) || { echo "STAT_FAIL"; return 1; }
    if echo "$xml" | grep -q "<name>$key</name>"; then
        if echo "$xml" | python3 -c "
import sys, xml.etree.ElementTree as ET
root = ET.fromstring(sys.stdin.read())
for s in root.iter('stream'):
    n = s.find('name')
    p = s.find('publish')
    if n is not None and n.text == '$key':
        if p is not None and p.attrib.get('active') == 'true':
            sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
            echo "PUBLISHING"
            return 0
        fi
    fi
    echo "NOT_PUBLISHING"
    return 1
}

# ── Docker container check ────────────────────────────────────
check_docker() {
    if ! command -v docker &>/dev/null; then return; fi
    echo -e "\n${BOLD}── Docker Containers ────────────────────────────────────────${NC}"
    docker ps --format "  {{.Names}}\t{{.Status}}\t{{.Ports}}" 2>/dev/null \
        | grep -E "nginx-rtmp|srs" \
        || echo "  (no streaming containers running)"
}

# ── FFmpeg fallback processes ─────────────────────────────────
check_fallback_procs() {
    echo -e "\n${BOLD}── Fallback Processes ───────────────────────────────────────${NC}"
    pids=$(pgrep -a ffmpeg 2>/dev/null | grep "stream_loop" || true)
    if [[ -z "$pids" ]]; then
        echo "  No FFmpeg fallback processes running"
    else
        echo "$pids" | while IFS= read -r line; do
            echo "  $line"
        done
    fi
}

# ── Main ─────────────────────────────────────────────────────
echo -e "\n${BOLD}═══════════════════════════════════════════════════════════════${NC}"
echo -e "${BOLD}  Streaming Stack Healthcheck  $(date -u '+%Y-%m-%d %H:%M:%S UTC')${NC}"
echo -e "${BOLD}═══════════════════════════════════════════════════════════════${NC}"

STAT_URL=$(python3 -c "import json; c=json.load(open('$CONFIG')); print(c['server'].get('stat_url','http://127.0.0.1:8080/stat'))")

# Read channels
mapfile -t CHANNELS < <(python3 -c "
import json
cfg = json.load(open('$CONFIG'))
for c in cfg['channels']:
    if c.get('enabled', True):
        name = c['channel_name']
        if not '$TARGET' or name == '$TARGET':
            print(c['channel_name'], c['hls_url'], c.get('rtmp_key', c['channel_name']))
")

echo -e "\n${BOLD}── Channels ─────────────────────────────────────────────────${NC}"
printf "  %-18s %-12s %-12s\n" "CHANNEL" "HLS" "RTMP"
printf "  %-18s %-12s %-12s\n" "──────────────────" "────────────" "────────────"

for entry in "${CHANNELS[@]}"; do
    read -r ch_name hls_url rtmp_key <<< "$entry"

    hls_result=$(check_hls "$hls_url" 2>/dev/null && echo "OK" || echo "FAIL")
    rtmp_result=$(check_rtmp "$STAT_URL" "$rtmp_key" 2>/dev/null || echo "FAIL")

    if [[ "$hls_result" == *"FRESH"* || "$hls_result" == "OK"* ]]; then
        hls_col="${GREEN}${hls_result}${NC}"
    elif [[ "$hls_result" == *"STALE"* ]]; then
        hls_col="${YELLOW}${hls_result}${NC}"
    else
        hls_col="${RED}${hls_result}${NC}"
    fi

    if [[ "$rtmp_result" == "PUBLISHING" ]]; then
        rtmp_col="${GREEN}${rtmp_result}${NC}"
    else
        rtmp_col="${RED}${rtmp_result}${NC}"
    fi

    printf "  %-18s ${hls_col}%-$(( 12 - ${#hls_result} ))s ${rtmp_col}%-$(( 12 - ${#rtmp_result} ))s\n" \
        "$ch_name" "" ""
done

check_docker
check_fallback_procs
print_system

echo -e "\n${BOLD}─────────────────────────────────────────────────────────────${NC}\n"
