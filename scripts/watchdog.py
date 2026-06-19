#!/usr/bin/env python3
"""
Stream Watchdog
───────────────
Monitors RTMP/HLS health for each enabled channel.
Starts the FFmpeg standby loop when the live source drops.
Stops the standby loop when the live source returns.

Usage:
  python watchdog.py              # run daemon (foreground)
  python watchdog.py --status     # print current status and exit
  python watchdog.py --restart channel1
  python watchdog.py --stop-fallback channel1

Optional FastAPI dashboard (install extras first):
  pip install fastapi uvicorn
  python watchdog.py --api        # runs daemon + HTTP API on :8888
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

# ── Config ────────────────────────────────────────────────────
CONFIG_FILE = Path(__file__).parent.parent / "config" / "channels.json"
LOG_BASE    = Path("/var/log/stream-watchdog")
SCRIPTS_DIR = Path(__file__).parent

# ── Data model ───────────────────────────────────────────────
@dataclass
class ChannelState:
    name: str
    config: dict
    status: str = "unknown"       # live | standby | offline | unknown
    last_segment_time: float = 0.0
    fallback_pid: Optional[int] = None
    fallback_proc: Optional[subprocess.Popen] = None
    restart_count: int = 0
    last_restart: float = 0.0
    last_check: float = 0.0
    uptime_start: Optional[float] = None
    log: Optional[logging.Logger] = None


# ── Helpers ──────────────────────────────────────────────────
def load_config(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def setup_logger(channel_name: str, log_dir: Path) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{channel_name}.log"
    logger = logging.getLogger(channel_name)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(log_file)
        fh.setLevel(logging.DEBUG)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s",
                                datefmt="%Y-%m-%dT%H:%M:%S")
        fh.setFormatter(fmt)
        ch.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(ch)
    return logger


def fetch_url(url: str, timeout: int = 5) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def parse_rtmp_stat(stat_url: str, channel_key: str) -> bool:
    """Return True if the channel key has an active publisher in the RTMP stat XML."""
    body = fetch_url(stat_url, timeout=4)
    if not body:
        return False
    try:
        root = ET.fromstring(body)
        for stream in root.iter("stream"):
            name_el = stream.find("name")
            publish_el = stream.find("publish")
            if name_el is not None and name_el.text == channel_key:
                if publish_el is not None and publish_el.attrib.get("active") == "true":
                    return True
    except ET.ParseError:
        pass
    return False


def check_hls_freshness(hls_url: str, stale_threshold: int) -> tuple[bool, float]:
    """
    Returns (is_fresh, last_segment_age_seconds).
    Fetches the m3u8 and checks the most recent TS segment's Last-Modified header.
    """
    body = fetch_url(hls_url)
    if not body:
        return False, float("inf")

    text = body.decode("utf-8", errors="ignore")
    # Extract TS segment filenames
    segments = [line.strip() for line in text.splitlines()
                if line.strip() and not line.startswith("#")]
    if not segments:
        return False, float("inf")

    last_seg = segments[-1]
    base = hls_url.rsplit("/", 1)[0]
    seg_url = f"{base}/{last_seg}" if not last_seg.startswith("http") else last_seg

    try:
        req = urllib.request.Request(seg_url, method="HEAD")
        with urllib.request.urlopen(req, timeout=4) as r:
            last_mod = r.headers.get("Last-Modified")
            if last_mod:
                from email.utils import parsedate_to_datetime
                ts = parsedate_to_datetime(last_mod).timestamp()
                age = time.time() - ts
                return age < stale_threshold, age
    except Exception:
        pass

    # Fallback: just check that the playlist downloaded – not ideal but non-null
    return True, 0.0


# ── Fallback process management ──────────────────────────────
def start_fallback(state: ChannelState, cfg: dict) -> bool:
    """Launch FFmpeg standby loop → RTMP. Returns True if started."""
    if state.fallback_proc and state.fallback_proc.poll() is None:
        return False  # already running

    standby = Path(__file__).parent.parent / state.config["standby_file"]
    if not standby.exists():
        state.log.error("Standby file not found: %s", standby)
        return False

    rtmp_url = state.config["rtmp_url"]
    br       = state.config.get("bitrate", 2000)
    abr      = state.config.get("audio_bitrate", 128)
    fps      = state.config.get("fps", 30)
    gop      = fps * 2   # keyframe every 2 s

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-re",                        # realtime playback rate
        "-stream_loop", "-1",         # infinite loop
        "-i", str(standby),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-tune", "zerolatency",
        "-b:v", f"{br}k",
        "-maxrate", f"{br}k",
        "-bufsize", f"{br * 2}k",
        "-r", str(fps),
        "-g", str(gop),
        "-c:a", "aac",
        "-b:a", f"{abr}k",
        "-ar", "44100",
        "-f", "flv",
        # Reconnect flags – auto-reconnect if the RTMP server blips
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        rtmp_url,
    ]

    log_file = LOG_BASE / f"{state.name}_fallback.log"
    with open(log_file, "a") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf)

    state.fallback_proc = proc
    state.fallback_pid  = proc.pid
    state.log.info("Fallback started (PID %d) → %s", proc.pid, rtmp_url)
    return True


def stop_fallback(state: ChannelState) -> bool:
    """Gracefully stop the fallback FFmpeg process."""
    if not state.fallback_proc:
        return False
    if state.fallback_proc.poll() is not None:
        state.fallback_proc = None
        return False
    state.log.info("Stopping fallback (PID %d)", state.fallback_proc.pid)
    state.fallback_proc.send_signal(signal.SIGTERM)
    try:
        state.fallback_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        state.fallback_proc.kill()
    state.fallback_proc = None
    state.fallback_pid  = None
    return True


# ── Per-channel monitoring loop ───────────────────────────────
def monitor_channel(state: ChannelState, server_cfg: dict, watchdog_cfg: dict,
                    stop_event: threading.Event):
    interval   = watchdog_cfg.get("check_interval_seconds", 10)
    stale_thr  = watchdog_cfg.get("stale_segment_threshold_seconds", 20)
    max_rst    = watchdog_cfg.get("max_restart_attempts", 5)
    cooldown   = watchdog_cfg.get("restart_cooldown_seconds", 30)
    stat_url   = server_cfg.get("stat_url", "http://127.0.0.1:8080/stat")
    hls_url    = state.config["hls_url"]
    rtmp_key   = state.config.get("rtmp_key", state.name)

    state.log.info("Monitor started for %s", state.name)

    while not stop_event.is_set():
        now = time.time()
        state.last_check = now

        # 1. Check RTMP publisher active
        rtmp_live = parse_rtmp_stat(stat_url, rtmp_key)

        # 2. Check HLS segment freshness
        hls_fresh, seg_age = check_hls_freshness(hls_url, stale_thr)
        if hls_fresh:
            state.last_segment_time = now

        fallback_running = (state.fallback_proc is not None and
                            state.fallback_proc.poll() is None)

        # 3. Determine status
        if rtmp_live and hls_fresh:
            if fallback_running:
                state.log.info("Live source restored – stopping fallback")
                stop_fallback(state)
            state.status = "live"
            if state.uptime_start is None:
                state.uptime_start = now

        elif fallback_running:
            state.status = "standby"
            state.uptime_start = None
            # Check if fallback process itself died
            if state.fallback_proc.poll() is not None:
                state.log.warning("Fallback process died – restarting")
                state.fallback_proc = None
                _maybe_restart_fallback(state, cooldown, max_rst)

        else:
            # Source down and no fallback → start fallback
            state.status = "offline"
            state.uptime_start = None
            state.log.warning("Source down (rtmp=%s hls_fresh=%s age=%.1fs) – starting fallback",
                              rtmp_live, hls_fresh, seg_age)
            _maybe_restart_fallback(state, cooldown, max_rst)

        stop_event.wait(interval)

    # Clean shutdown
    stop_fallback(state)
    state.log.info("Monitor stopped for %s", state.name)


def _maybe_restart_fallback(state: ChannelState, cooldown: float, max_rst: int):
    now = time.time()
    if state.restart_count >= max_rst:
        if now - state.last_restart > cooldown * 6:
            state.restart_count = 0  # reset counter after extended cooldown
        else:
            state.log.error("Max restart attempts (%d) reached – standing by", max_rst)
            return
    if now - state.last_restart < cooldown:
        return
    state.restart_count += 1
    state.last_restart = now
    start_fallback(state, {})  # config is on state.config


# ── Status display ────────────────────────────────────────────
def print_status(states: Dict[str, ChannelState]):
    now = time.time()
    print("\n── Stream Status ─────────────────────────────────────────")
    print(f"  {'CHANNEL':<18} {'STATUS':<10} {'UPTIME':>10} {'RESTARTS':>9} {'LAST CHECK':>12}")
    print(f"  {'─'*18} {'─'*10} {'─'*10} {'─'*9} {'─'*12}")
    for name, s in states.items():
        uptime = ""
        if s.uptime_start:
            sec = int(now - s.uptime_start)
            h, m = divmod(sec // 60, 60)
            uptime = f"{h:02d}:{m:02d}:{sec%60:02d}"
        last = datetime.fromtimestamp(s.last_check).strftime("%H:%M:%S") if s.last_check else "–"
        icon = {"live": "●", "standby": "◌", "offline": "✗", "unknown": "?"}.get(s.status, "?")
        print(f"  {icon} {name:<17} {s.status:<10} {uptime:>10} {s.restart_count:>9} {last:>12}")
    print("──────────────────────────────────────────────────────────\n")


# ── Optional FastAPI dashboard ────────────────────────────────
def start_api(states: Dict[str, ChannelState], port: int = 8888):
    try:
        from fastapi import FastAPI
        import uvicorn
    except ImportError:
        print("FastAPI not installed. Run: pip install fastapi uvicorn")
        return

    app = FastAPI(title="Stream Watchdog")

    @app.get("/status")
    def status():
        now = time.time()
        return {
            name: {
                "status": s.status,
                "restart_count": s.restart_count,
                "fallback_running": (s.fallback_proc is not None and
                                     s.fallback_proc.poll() is None),
                "last_check": s.last_check,
                "uptime_seconds": int(now - s.uptime_start) if s.uptime_start else None,
            }
            for name, s in states.items()
        }

    @app.get("/channels")
    def channels():
        return {name: s.config for name, s in states.items()}

    @app.post("/restart/{channel_name}")
    def restart(channel_name: str):
        if channel_name not in states:
            return {"error": "not found"}
        s = states[channel_name]
        stop_fallback(s)
        s.restart_count = 0
        s.last_restart  = 0
        return {"ok": True, "channel": channel_name}

    thread = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "0.0.0.0", "port": port, "log_level": "warning"},
        daemon=True,
    )
    thread.start()
    print(f"API dashboard: http://0.0.0.0:{port}/status")


# ── Main ──────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Stream Watchdog")
    parser.add_argument("--config",    default=str(CONFIG_FILE))
    parser.add_argument("--status",    action="store_true", help="Print status and exit")
    parser.add_argument("--restart",   metavar="CHANNEL",   help="Restart fallback for channel")
    parser.add_argument("--stop-fallback", metavar="CHANNEL")
    parser.add_argument("--api",       action="store_true", help="Enable FastAPI dashboard")
    args = parser.parse_args()

    cfg        = load_config(Path(args.config))
    server_cfg = cfg["server"]
    wd_cfg     = cfg["watchdog"]
    log_dir    = Path(wd_cfg.get("log_dir", "/var/log/stream-watchdog"))

    channels   = [c for c in cfg["channels"] if c.get("enabled", True)]
    states: Dict[str, ChannelState] = {}

    for ch in channels:
        name = ch["channel_name"]
        logger = setup_logger(name, log_dir)
        states[name] = ChannelState(name=name, config=ch, log=logger)

    if args.status:
        # Quick one-shot status: check HLS freshness for each channel
        stale_thr = wd_cfg.get("stale_segment_threshold_seconds", 20)
        for name, s in states.items():
            hls_fresh, age = check_hls_freshness(s.config["hls_url"], stale_thr)
            rtmp_live = parse_rtmp_stat(server_cfg.get("stat_url"), s.config.get("rtmp_key", name))
            s.status = "live" if (rtmp_live and hls_fresh) else ("standby" if hls_fresh else "offline")
            s.last_check = time.time()
        print_status(states)
        return

    if args.restart:
        ch = args.restart
        if ch not in states:
            print(f"Unknown channel: {ch}")
            sys.exit(1)
        stop_fallback(states[ch])
        states[ch].restart_count = 0
        states[ch].last_restart  = 0
        print(f"Restarted fallback for {ch}")
        return

    if args.stop_fallback:
        ch = args.stop_fallback
        if ch not in states:
            print(f"Unknown channel: {ch}")
            sys.exit(1)
        stop_fallback(states[ch])
        print(f"Stopped fallback for {ch}")
        return

    # ── Daemon mode ──────────────────────────────────────────
    stop_event = threading.Event()

    def _sighandler(sig, frame):
        print("\nShutting down watchdog…")
        stop_event.set()

    signal.signal(signal.SIGTERM, _sighandler)
    signal.signal(signal.SIGINT,  _sighandler)

    threads = []
    for name, state in states.items():
        t = threading.Thread(
            target=monitor_channel,
            args=(state, server_cfg, wd_cfg, stop_event),
            name=f"monitor-{name}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    if args.api:
        start_api(states)

    # Periodic status print (every 60 s)
    while not stop_event.is_set():
        stop_event.wait(60)
        if not stop_event.is_set():
            print_status(states)

    for t in threads:
        t.join(timeout=10)


if __name__ == "__main__":
    main()
