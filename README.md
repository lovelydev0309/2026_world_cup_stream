# Streaming Stack – Production RTMP → HLS Re-streaming

A Docker-based, 24/7-capable IPTV re-streaming infrastructure.
One channel proof-of-concept, structured to scale to 10–20 channels on GCP.

> **Compliance notice**  
> This system supports **authorized and licensed video sources only**.  
> DRM bypass, Widevine key extraction, anti-capture circumvention, and unauthorized
> stream capture are explicitly out of scope and **not implemented**.  
> If your source is DRM-protected, you must obtain a direct authorized ingest URL
> (e.g., a provider-issued encoder credential) before using this stack.

---

## Table of Contents

1. [Architecture](#architecture)
2. [Quick Start – 5-Minute Setup](#quick-start)
3. [OBS Studio Configuration](#obs-configuration)
4. [Testing Commands](#testing-commands)
5. [Standby Fallback System](#standby-fallback-system)
6. [Watchdog & Monitoring](#watchdog--monitoring)
7. [Systemd Services (Production)](#systemd-services)
8. [Multi-Channel Scaling](#multi-channel-scaling)
9. [GCP Sizing Guide](#gcp-sizing-guide)
10. [Performance & HLS Tuning](#performance--hls-tuning)
11. [Troubleshooting Guide](#troubleshooting-guide)

---

## Architecture

```
OBS (Windows)                Ubuntu Media Server
─────────────     RTMP       ──────────────────────────
 Authorized  ──────────────▶  Nginx-RTMP (Docker)
 source                       │
 capture                      ├── /var/www/hls/channel1/
                              │       index.m3u8
                              │       *.ts  (2s segments)
                              │
                              └── HTTP :8080
                                    /hls/channel1/index.m3u8
                                    /player/channel1.html
                                    /stat  (XML stats)
                                    /healthz

Fallback:  FFmpeg loop (standby.mp4) ──▶ same RTMP endpoint
Watchdog:  watchdog.py monitors HLS freshness + RTMP publisher
           auto-starts/stops fallback when source drops/returns
```

---

## Quick Start

### Prerequisites

```bash
# Ubuntu 22.04 LTS
sudo apt update
sudo apt install -y docker.io docker-compose-plugin ffmpeg python3 python3-pip curl bc
sudo systemctl enable --now docker
sudo usermod -aG docker $USER   # re-login after this
pip3 install fastapi uvicorn    # optional – for the API dashboard
```

### 1. Clone / copy the project

```bash
sudo cp -r streaming-stack /opt/streaming-stack
cd /opt/streaming-stack
```

### 2. Edit server IP in channels.json

```bash
nano config/channels.json
# Set "rtmp_host" and "hls_base_url" to your server's public IP
```

### 3. Add your standby video

```bash
# Copy any valid MP4 (used when live source drops)
cp /path/to/your/standby.mp4 standby/standby.mp4

# Generate a 60-second test standby if you have none:
ffmpeg -f lavfi -i color=c=black:s=1920x1080:r=30 \
       -f lavfi -i sine=frequency=440 \
       -t 60 -c:v libx264 -preset ultrafast -c:a aac \
       standby/standby.mp4
```

### 4. Build and start

```bash
cd /opt/streaming-stack
docker compose up -d --build
docker compose ps        # verify nginx-rtmp is Up
docker compose logs -f   # tail logs
```

### 5. Verify the server is alive

```bash
curl http://localhost:8080/healthz          # → ok
curl http://localhost:8080/stat             # → RTMP XML stats
```

---

## OBS Configuration

### Source setup (Windows)

1. **Video source** – add a **Window Capture** or **Game Capture** targeting
   the authorized IPTV app / browser player.
2. **Audio** – if the app outputs to a virtual audio cable:
   - Install **VB-Audio Virtual Cable** (free, Windows)
   - Set your player audio output → Cable Input
   - In OBS: Audio Settings → Desktop Audio → Cable Output
3. **Display capture note** – avoid full display capture on RDP sessions;
   the RDP virtual GPU often produces a black frame. Use a **physical display**
   or a **virtual display driver** (e.g., IddSampleDriver) to get a persistent
   active session.

### OBS Output settings

| Setting | Value |
|---------|-------|
| Output Mode | Advanced |
| Type | Custom… FFMPEG |
| **Service** | Custom |
| **Server** | `rtmp://YOUR_SERVER_IP:1935/live` |
| **Stream Key** | `channel1` |
| Encoder | x264 (CPU) or NVENC (GPU) |
| Rate Control | CBR |
| **Bitrate** | 3000 kbps (1080p30) / 2500 kbps (720p30) |
| **Keyframe Interval** | 2 seconds (= 60 frames at 30 fps) |
| CPU Usage Preset | veryfast |
| Profile | high |
| Tune | zerolatency |
| **Resolution** | 1920×1080 |
| **FPS** | 30 |

### Audio settings

| Setting | Value |
|---------|-------|
| Sample Rate | 44100 Hz |
| Channels | Stereo |
| Audio Bitrate | 128 kbps (per track) |
| Encoder | AAC |

### Windows Server stability tips

- **Disable screensaver and sleep** – `powercfg /change standby-timeout-ac 0`
- **Disable Windows Update auto-restart** – configure Active Hours
- **Run OBS as a service** – use **NSSM** (Non-Sucking Service Manager) to keep
  OBS running after logout
- **GPU memory** – if using NVENC, ensure GPU driver is current; avoid running
  OBS in a headless session without a virtual display driver
- **CPU throttling** – disable turbo-boost limits in power plan (High Performance)
- **Monitor OBS with** – Task Scheduler restart task + a watchdog ping to the
  RTMP stat endpoint every 30 s

---

## Testing Commands

### Start the media server

```bash
cd /opt/streaming-stack
docker compose up -d
```

### Push a test MP4 to RTMP (simulates OBS)

```bash
# One-shot: play a file once
ffmpeg -re -i standby/standby.mp4 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -b:v 3000k -maxrate 3000k -bufsize 6000k \
  -r 30 -g 60 \
  -c:a aac -b:a 128k -ar 44100 \
  -f flv rtmp://localhost:1935/live/channel1
```

### Push standby.mp4 as an infinite loop (simulates fallback)

```bash
ffmpeg -re -stream_loop -1 -i standby/standby.mp4 \
  -c:v libx264 -preset veryfast -tune zerolatency \
  -b:v 3000k -maxrate 3000k -bufsize 6000k \
  -r 30 -g 60 \
  -c:a aac -b:a 128k -ar 44100 \
  -f flv \
  -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 \
  rtmp://localhost:1935/live/channel1
```

### Probe the HLS stream

```bash
ffprobe -v quiet -print_format json -show_streams \
  http://localhost:8080/hls/channel1/index.m3u8
```

### Test HLS latency and segment freshness

```bash
# Download and inspect the playlist
curl -s http://localhost:8080/hls/channel1/index.m3u8

# Check if segments update (run twice ~3 s apart)
curl -sI http://localhost:8080/hls/channel1/index.m3u8 | grep Last-Modified
```

### Open the browser player

```
http://YOUR_SERVER_IP:8080/player/channel1.html
```

### Watchdog status check

```bash
python3 scripts/watchdog.py --status
```

### Restart fallback for one channel

```bash
python3 scripts/watchdog.py --restart channel1
# or via systemd:
sudo systemctl restart stream-fallback@channel1
```

### View logs

```bash
# Watchdog log
tail -f /var/log/stream-watchdog/channel1.log

# Fallback FFmpeg log
tail -f /var/log/stream-watchdog/channel1_fallback.log

# Docker container log
docker compose logs -f nginx-rtmp

# Systemd journal
journalctl -u stream-watchdog@channel1 -f
```

### Run the full healthcheck

```bash
bash scripts/healthcheck.sh
bash scripts/healthcheck.sh channel1   # single channel
```

---

## Standby Fallback System

```
Source UP   → OBS pushes RTMP → Nginx-RTMP → HLS
Source DOWN → watchdog.py detects stale HLS (>20 s)
           → starts FFmpeg (start_fallback.sh channel1)
           → FFmpeg loops standby.mp4 → same RTMP key
           → HLS URL stays the same; browser player never blacks out
Source UP   → watchdog.py detects RTMP publisher back
           → kills fallback FFmpeg
           → OBS stream takes over
```

### Manual fallback test

```bash
# 1. Ensure no live ingest is running
# 2. Manually start fallback
bash scripts/start_fallback.sh channel1

# 3. Verify HLS is serving standby
curl -s http://localhost:8080/hls/channel1/index.m3u8

# 4. Kill fallback (simulate OBS returning)
pkill -f "start_fallback.sh channel1"
```

---

## Watchdog & Monitoring

### Daemon mode

```bash
python3 scripts/watchdog.py
```

### Status snapshot

```bash
python3 scripts/watchdog.py --status
```

### Optional FastAPI dashboard

```bash
pip3 install fastapi uvicorn
python3 scripts/watchdog.py --api
# Endpoints:
#   GET  http://localhost:8888/status
#   GET  http://localhost:8888/channels
#   POST http://localhost:8888/restart/channel1
```

### What the watchdog monitors

| Check | Method | Threshold |
|-------|--------|-----------|
| RTMP publisher active | Parse `/stat` XML | Immediate |
| HLS segment freshness | HEAD last .ts segment | >20 s → stale |
| Fallback process alive | `poll()` | Immediate |
| Restart rate limiting | Cooldown timer | 30 s min between restarts |
| Max restarts | Counter reset after 6× cooldown | 5 attempts |

---

## Systemd Services

### Install

```bash
sudo cp systemd/streaming-stack.service  /etc/systemd/system/
sudo cp systemd/stream-fallback@.service /etc/systemd/system/
sudo cp systemd/stream-watchdog@.service /etc/systemd/system/

# Create a dedicated user
sudo useradd -r -s /bin/false -d /opt/streaming-stack stream
sudo chown -R stream:stream /opt/streaming-stack
sudo systemctl daemon-reload
```

### Enable on boot

```bash
sudo systemctl enable streaming-stack
sudo systemctl enable stream-fallback@channel1
sudo systemctl enable stream-watchdog@channel1
```

### Start all

```bash
sudo systemctl start streaming-stack
sleep 5
sudo systemctl start stream-fallback@channel1
sudo systemctl start stream-watchdog@channel1
```

### Status

```bash
systemctl status streaming-stack
systemctl status stream-fallback@channel1
systemctl status stream-watchdog@channel1
```

---

## Multi-Channel Scaling

### Add a new channel

1. Edit `config/channels.json` – add an entry with `"enabled": true`
2. Copy the player template:
   ```bash
   sed -e 's/{{CHANNEL_NAME}}/channel2/g' \
       -e 's|{{HLS_URL}}|http://SERVER_IP:8080/hls/channel2/index.m3u8|g' \
       player/channel-template.html > player/channel2.html
   ```
3. Enable systemd units:
   ```bash
   sudo systemctl enable stream-fallback@channel2 stream-watchdog@channel2
   sudo systemctl start  stream-fallback@channel2 stream-watchdog@channel2
   ```

No nginx.conf changes needed – Nginx-RTMP auto-creates HLS for any key published
to `rtmp://HOST/live/<anything>`.

---

## GCP Sizing Guide

### Instance type recommendations

| Channels | CPU Encoding | GPU Encoding | Machine Type | RAM | Boot Disk | Bandwidth |
|----------|-------------|-------------|--------------|-----|-----------|-----------|
| 1 test   | CPU only    | –           | e2-standard-2 | 8 GB | 50 GB SSD | 100 Mbps |
| 5        | CPU only    | –           | c2-standard-8 | 32 GB | 100 GB SSD | 1 Gbps |
| 10–20    | CPU only    | –           | c2-standard-16 | 64 GB | 200 GB SSD | 2 Gbps |
| 10–20    | GPU (NVENC) | n1-standard-8 + T4 | 30 GB | 200 GB SSD | 2 Gbps |

### CPU vs GPU encoding

**Use CPU (libx264)** when:
- Fewer than 8 channels at 1080p30
- Budget matters (no GPU surcharge)
- `veryfast` preset keeps CPU usage at ~10–15% per 1080p30 channel on c2

**Use GPU (h264_nvenc)** when:
- 10+ channels simultaneously
- CPU is at >80% from encoding load
- Latency must drop below 500 ms (NVENC has lower encode latency)

Change the encoder in `start_fallback.sh` / OBS:
```
-c:v h264_nvenc -preset p3 -tune ll -b:v 3000k
```

### HLS segment settings for stable website playback

| Parameter | Recommended | Notes |
|-----------|-------------|-------|
| `hls_fragment` | 2s | Balance latency vs. stability |
| `hls_playlist_length` | 10s | 5 segments in playlist |
| `hls_cleanup on` | yes | Prevent disk fill |
| `hls_continuous on` | yes | No playlist gap on OBS reconnect |
| keyframe interval | 2s (= fps×2) | Must match fragment length |
| hls.js `liveSyncDurationCount` | 3 | 6 s behind live edge |

### Bandwidth estimates

| Profile | Per-channel | 5 channels | 20 channels |
|---------|-------------|------------|-------------|
| 1080p30 @ 3 Mbps | 3.3 Mbps | 16.5 Mbps | 66 Mbps |
| 720p30 @ 2 Mbps | 2.2 Mbps | 11 Mbps | 44 Mbps |

Add 20% overhead for HLS HTTP delivery multiplied by concurrent viewers.

### GCP startup script (paste into VM metadata)

```bash
#!/bin/bash
apt-get update -y
apt-get install -y docker.io docker-compose-plugin ffmpeg python3 python3-pip curl
systemctl enable --now docker
pip3 install fastapi uvicorn
cp -r /tmp/streaming-stack /opt/streaming-stack
cd /opt/streaming-stack
docker compose up -d --build
```

---

## Performance & HLS Tuning

### nginx.conf adjustments for scale

```nginx
worker_processes auto;          # one per vCPU
rtmp {
  server {
    chunk_size 4096;            # 4096 is good for low-latency
    buflen 2s;                  # reduce to 1s for lower latency
  }
}
```

### FFmpeg encoding presets

| Preset | CPU Usage | Quality | Use case |
|--------|-----------|---------|----------|
| ultrafast | ~5% | low | Testing only |
| veryfast | ~15% | good | Production default |
| faster | ~25% | better | If CPU headroom allows |
| medium | ~40% | best | Offline transcode only |

### Disk I/O

HLS segments are tiny (2s × 3 Mbps ≈ 750 KB each).  
For 20 channels, peak disk write ≈ 20 × 750 KB/2s = 7.5 MB/s.  
A standard SSD handles this easily. Use `tmpfs` for the HLS directory
if you want to eliminate disk I/O entirely:

```yaml
# docker-compose.yml – mount HLS as tmpfs (lost on reboot)
volumes:
  - type: tmpfs
    target: /var/www/hls
    tmpfs:
      size: 512m
```

---

## Troubleshooting Guide

### Black screen / no video in browser

| Symptom | Cause | Fix |
|---------|-------|-----|
| Video element exists, spinner never stops | m3u8 URL not reachable | Check CORS headers, server IP, port 8080 open |
| m3u8 loads but segments 404 | HLS dir not mounted | Verify docker volume `./hls:/var/www/hls` |
| Black frames in video | OBS capturing RDP virtual desktop | Use physical display or virtual display driver |
| Works in VLC, not browser | CORS missing | Verify `add_header Access-Control-Allow-Origin "*"` in nginx |

### Freezing / buffering

| Symptom | Cause | Fix |
|---------|-------|-----|
| Freezes every ~30 s | Keyframe interval mismatch | Set OBS keyframe = 2s; `hls_fragment 2s` |
| Constant buffering | Bitrate exceeds upload bandwidth | Reduce OBS bitrate below 80% of upload |
| Segments go stale | OBS crashed | Watchdog restarts fallback automatically |
| High latency (>30 s) | Playlist window too large | Reduce `hls_playlist_length` to 6s |

### No audio

| Symptom | Cause | Fix |
|---------|-------|-----|
| Video plays, no audio | Browser autoplay policy | Unmute video element (it's muted by default) |
| Audio in OBS, not in stream | Wrong audio device selected | Check Desktop Audio in OBS settings |
| Audio distortion | Sample rate mismatch | Set 44100 Hz in both OBS and FFmpeg |

### RTMP connection refused

```bash
# Check container is running
docker compose ps

# Check port is open
ss -tlnp | grep 1935

# Test RTMP manually
ffmpeg -re -i standby/standby.mp4 -f flv rtmp://localhost:1935/live/test
```

### OBS disconnect / reconnect loop

- Check OBS log for `Connection timed out`
- Ensure server firewall allows inbound TCP 1935
- On GCP: add a VPC firewall rule allowing TCP 1935 from OBS source IP
- Increase OBS reconnect delay to 5–10 s

### HLS expires / playlist gone after reconnect

`hls_continuous on` prevents this. If still happening:
```bash
docker compose restart nginx-rtmp
```
The `hls_cleanup on` + `hls_continuous on` combination means the server
reuses the same m3u8 filename across reconnects.

### Expired HLS – player shows old segments

```bash
# Force a hard reload in browser: Ctrl+Shift+R
# Or clear nginx proxy cache (if behind a CDN/proxy)
```

### Watchdog not detecting live stream

```bash
# Check stat endpoint is reachable
curl http://localhost:8080/stat | grep -A5 "<stream>"

# Check HLS URL directly
curl -I http://localhost:8080/hls/channel1/index.m3u8
```

### CORS errors in browser console

```
Access to fetch blocked by CORS policy
```
Fix: verify your nginx.conf has `add_header Access-Control-Allow-Origin "*";`
under the `/hls` location block AND the server is not behind a CDN that strips headers.

### Checking if the fallback is running

```bash
pgrep -a ffmpeg | grep stream_loop
# or
systemctl status stream-fallback@channel1
```

---

## File Structure

```
streaming-stack/
├── docker-compose.yml          ← start here
├── nginx-rtmp/
│   ├── Dockerfile
│   └── nginx.conf              ← RTMP + HLS + HTTP config
├── srs/
│   └── srs.conf                ← alternative media server
├── hls/                        ← HLS segments (created at runtime)
├── standby/
│   └── standby.mp4             ← drop your standby video here
├── scripts/
│   ├── watchdog.py             ← daemon + --status + --api
│   ├── start_fallback.sh       ← FFmpeg loop (called by systemd)
│   └── healthcheck.sh          ← quick CLI health snapshot
├── config/
│   └── channels.json           ← all channel settings
├── systemd/
│   ├── streaming-stack.service ← Docker Compose boot service
│   ├── stream-fallback@.service← per-channel fallback template
│   └── stream-watchdog@.service← per-channel watchdog template
├── player/
│   ├── channel-template.html   ← hls.js template
│   └── channel1.html           ← ready-to-open player
└── README.md
```
