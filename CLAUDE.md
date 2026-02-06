# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Device Pilot is a motion/event detection and video capture system that works on both Mac (development) and Raspberry Pi (production). It connects to RTSP cameras, uses OpenCV for motion and light detection, and automatically records triggered events to MP4 files. Supports overlapping events (new motion during cooldown creates a second video with pre-roll overlap).

## Setup

**Mac (one command):**
```bash
make setup-mac
```

**Raspberry Pi (one command):**
```bash
make setup-pi
```

**Manual setup:**
```bash
# Mac system dependencies
brew install ffmpeg fswatch

# Raspberry Pi system dependencies
sudo apt install ffmpeg inotify-tools python3-opencv

# Python dependencies (both platforms)
pip install -e ".[dev]"
```

## Running

**Run the system:**
```bash
python -m src --verbose

# With custom settings
python -m src --pre-roll 15 --cooldown 10 --verbose
```

**Run tests:**
```bash
make test              # All tests
make test-verbose      # Verbose output
pytest -k "scenario"   # By name pattern
```

## Architecture

```
src/
├── config.py           # Configuration from env/CLI (loads .env)
├── platform.py         # Platform abstraction (Mac: fswatch, Linux: inotifywait)
├── detector.py         # Motion/light detection (OpenCV)
├── session.py          # Session state machine (RECORDING → COOLDOWN → COMPLETED)
├── session_manager.py  # Multi-session orchestration (handles overlapping events)
├── buffer.py           # HLS buffer management (FFmpeg)
├── recorder.py         # Clip draining and MP4 concatenation
└── main.py             # Entry point
```

**Data flow:**
```
RTSP Camera
    │
    ├──► HLSBuffer (buffer.py)
    │    └── FFmpeg captures MAIN stream → clip_*.ts segments
    │
    └──► Detector (detector.py)
         └── Analyzes SUB stream for motion/light
         └── Triggers SessionManager

SessionManager (session_manager.py)
    │
    ├── on_motion_detected() → starts new Session or extends existing
    ├── on_no_motion() → Session enters COOLDOWN
    └── tick() → finalizes Sessions after cooldown expires

Recorder (recorder.py)
    └── Copies pre-roll + drains live clips → FFmpeg concat → MP4
```

**Session state machine:**
```
RECORDING ──(no motion)──► COOLDOWN ──(cooldown expires)──► FINALIZING ──► COMPLETED
    ▲                          │
    └───(motion detected)──────┘
```

**Overlapping events (Scenario 2):**
- Event A recording → A enters cooldown → new motion detected
- A continues cooldown, B starts with pre-roll (overlaps A's end)
- Result: Two MP4 files with overlapping footage

## Configuration

**Environment variables** (can be set in `.env` file):
- `RTSP_URL_MAIN` - High-res stream URL for recording
- `RTSP_URL_SUB` - Low-res stream URL for detection
- `PILOT_PRE_ROLL_SECONDS` - Pre-roll duration (default: 3)
- `PILOT_COOLDOWN_SECONDS` - Cooldown duration (default: 3)
- `PILOT_STARTUP_DELAY_SECONDS` - Wait before enabling detection (default: 10)
- `PILOT_MIN_MOTION_SECONDS` - Minimum continuous motion to trigger (default: 0.5)
- `PILOT_MOTION_THRESHOLD` - Motion sensitivity 0-1 (default: 0.02)
- `PILOT_LIGHT_JUMP_THRESHOLD` - Light sensitivity 0-255 (default: 30)
- `PILOT_MAX_RECONNECT_DELAY` - Max delay between reconnection attempts (default: 60)
- `PILOT_BUFFER_DIR` - Buffer directory path (default: /tmp/device-pilot/buffer)
- `PILOT_SESSIONS_DIR` - Sessions directory path (default: /tmp/device-pilot/sessions)
- `PILOT_EVIDENCE_DIR` - Output directory path (default: ~/device-pilot-recordings)

**CLI arguments:** (override environment)
```
--pre-roll, --cooldown, --motion-threshold, --light-threshold
--buffer-dir, --sessions-dir, --evidence-dir
--rtsp-main, --rtsp-sub
-v/--verbose
```

## Platform Differences

| Feature | Mac | Raspberry Pi |
|---------|-----|--------------|
| File watcher | fswatch | inotifywait |
| Buffer dir | temp directory | RAM disk (tmpfs) |
| Detection | Auto-detected via `sys.platform` |

## Testing

Tests use pytest with dynamically generated test videos (requires FFmpeg).

**Test categories:**
- `test_session.py` - Session state machine
- `test_session_manager.py` - Multi-session scenarios (serial/overlapping)
- `test_detector.py` - Motion/light detection
- `test_recorder.py` - Clip management and concatenation
- `test_video_integrity.py` - I-frame alignment, timestamp continuity
- `test_integration.py` - Full pipeline tests

**Skip markers:**
- `@requires_ffmpeg` - Skipped if FFmpeg unavailable
- `@requires_ffprobe` - Skipped if FFprobe unavailable
- `@requires_fswatch` - Skipped if fswatch unavailable

## Key Implementation Details

**Motion detection smoothing:**
- Scores averaged over 15 frames (~0.5s at 30fps) to prevent false triggers
- Hysteresis: requires 30 consecutive low-motion frames (~1s) before "no motion"

**Pre-roll calculation:**
- HLS uses 5-second segments, pre-roll rounds up to whole segments
- Formula: `ceil(pre_roll_seconds / 5) * 5 = actual pre-roll`
- Example: requesting 3s pre-roll → 1 segment = 5s actual

**Network resilience:**
- Handles outages up to 5+ minutes with indefinite retry
- Exponential backoff on stream disconnection (1s → 2s → 4s → ... up to 30s)
- Auto-restarts HLS buffer after 10 consecutive failures or 2 minutes of outage
- Detector state reset after reconnection to prevent false triggers

**False positive prevention:**
- Startup delay (default 10s) lets camera stabilize before enabling detection
- Minimum motion duration (default 0.5s) filters brief artifacts/noise

**Camera requirements:**
- Configure GOP (keyframe interval) to 5 seconds (150 frames at 30fps)
- Each HLS segment must start with an I-frame for clean concatenation
