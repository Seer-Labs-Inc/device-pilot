# Device Pilot

Motion/event detection and video capture system for Raspberry Pi and Mac.

Connects to RTSP cameras, detects motion and light changes using OpenCV, and automatically records triggered events to MP4 files. Supports overlapping events (new motion during cooldown creates a second video with pre-roll overlap).

## Features

- **Dual-stream architecture**: Low-res stream for detection, high-res for recording
- **Pre-roll capture**: Records footage from before the event was detected
- **Overlapping events**: Multiple simultaneous recordings when new motion occurs during cooldown
- **Cross-platform**: Mac (development) and Raspberry Pi (production)
- **Configurable**: Timing, thresholds, and paths via environment or CLI

## Requirements

- Python 3.9+
- FFmpeg
- fswatch (Mac) or inotify-tools (Raspberry Pi)
- RTSP camera with dual stream support

## Setup

### Mac

```bash
make setup-mac
```

Or manually:
```bash
brew install ffmpeg fswatch
pip install -e ".[dev]"
```

### Raspberry Pi

```bash
make setup-pi
```

Or manually:
```bash
sudo apt install ffmpeg inotify-tools python3-opencv
pip install -e ".[dev]"
```

## Configuration

Create a `.env` file in the project root:

```env
# Reolink camera example
RTSP_URL_MAIN=rtsp://user:pass@192.168.x.x:554/h264Preview_01_main
RTSP_URL_SUB=rtsp://user:pass@192.168.x.x:554/h264Preview_01_sub

# Ubiquiti camera example (uncomment to use)
#RTSP_URL_MAIN=rtsps://192.168.x.x:7441/<high-stream-token>?enableSrtp
#RTSP_URL_SUB=rtsps://192.168.x.x:7441/<low-stream-token>?enableSrtp
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `RTSP_URL_MAIN` | High-res stream URL for recording | (required) |
| `RTSP_URL_SUB` | Low-res stream URL for detection | (required) |
| `PILOT_PRE_ROLL_SECONDS` | Pre-roll duration | 3 |
| `PILOT_COOLDOWN_SECONDS` | Cooldown duration | 3 |
| `PILOT_MOTION_THRESHOLD` | Motion sensitivity (0-1) | 0.02 |
| `PILOT_LIGHT_JUMP_THRESHOLD` | Light sensitivity (0-255) | 30 |
| `PILOT_BUFFER_DIR` | HLS buffer directory | /tmp/device-pilot/buffer |
| `PILOT_SESSIONS_DIR` | Session data directory | /tmp/device-pilot/sessions |
| `PILOT_EVIDENCE_DIR` | Output MP4 directory | ~/device-pilot-recordings |

## Running

```bash
# Basic run
python -m src --verbose

# With custom settings
python -m src --pre-roll 15 --cooldown 10 --verbose

# Override RTSP URLs via CLI
python -m src --rtsp-main "rtsp://..." --rtsp-sub "rtsp://..." --verbose
```

### CLI Options

```
--pre-roll SECONDS      Pre-roll duration (default: 3)
--cooldown SECONDS      Cooldown duration (default: 3)
--motion-threshold N    Motion sensitivity 0-1 (default: 0.02)
--light-threshold N     Light sensitivity 0-255 (default: 30)
--buffer-dir PATH       HLS buffer directory
--sessions-dir PATH     Session data directory
--evidence-dir PATH     Output MP4 directory
--rtsp-main URL         High-res RTSP stream
--rtsp-sub URL          Low-res RTSP stream
-v, --verbose           Enable verbose logging
```

## How It Works

1. **HLS Buffer**: FFmpeg continuously captures the main RTSP stream to rotating `.ts` segments
2. **Detection**: OpenCV analyzes the sub stream for motion (background subtraction) and light changes (brightness delta)
3. **Recording**: When motion is detected:
   - Pre-roll clips are copied from the buffer
   - Live clips are drained as they're created
   - On cooldown expiry, clips are concatenated to MP4

## Event Scenarios

Each recorded video is a continuous recording that includes:
- **Pre-roll**: Footage from before motion was detected
- **Active period**: While motion is being detected
- **Cooldown**: Footage after motion stops, until cooldown timer expires

### Scenario 1: Serial Events

Events are separate with enough time between them for independent pre-roll buffering.

```
                   "A" Start      "A" End                "B" Start      "B" End
                   (motion)     (no motion)              (motion)     (no motion)
                       |             |                       |             |
  timeline: ----[=====|=============|=====]-----//-----[====|=============|=====]----
                |     |             |     |            |    |             |     |
                |<--->|<----------->|<--->|            |<-->|<----------->|<--->|
              Pre-roll    Active   Cooldown          Pre-roll   Active   Cooldown
                |                       |              |                       |
                |<------- Video A ----->|              |<------- Video B ----->|
```

Result: Two separate MP4 files with no overlapping footage.

### Scenario 2: Overlapping Events

Event B starts during event A's cooldown period. B's pre-roll overlaps with A's recording.

```
                   "A" Start      "A" End   "B" Start                "B" End
                   (motion)     (no motion) (motion)               (no motion)
                       |             |          |                       |
  timeline: ----[=====|=============|=====]----:----[====|==============|=====]----
                |     |             |     :    |    |    |              |     |
                |<--->|<----------->|<----:--->|<-->|<-->|<------------>|<--->|
              Pre-roll    Active   Cooldown  Pre-roll   Active        Cooldown
                |                       | :    |                            |
                |<------- Video A ----->| :    |<--------- Video B -------->|
                                        : :    |
                                        |<---->|
                                      overlap region
```

Result: Two MP4 files where Video B's beginning (pre-roll) overlaps with Video A's end (cooldown). This ensures no footage is lost when events occur in quick succession.

## Testing

```bash
make test              # Run all tests
make test-verbose      # Verbose output
pytest -k "scenario"   # Run specific tests
```

## Technical Notes

### Motion Detection Smoothing

To prevent false triggers and video fragmentation, the detector uses:

- **Smoothing window**: Motion scores are averaged over 15 frames (~0.5 seconds at 30 FPS)
- **Hysteresis**: Once motion is detected, requires 30 consecutive low-motion frames (~1 second) before declaring "no motion"

This prevents brief dips in motion (person pausing, partial occlusion) from ending a recording prematurely.

### Pre-roll and Segment Duration

The HLS buffer uses 5-second segments. Pre-roll is calculated as whole segments, so:
- Requesting 3 seconds pre-roll → 1 segment = 5 seconds actual
- Requesting 8 seconds pre-roll → 2 segments = 10 seconds actual

Formula: `ceil(pre_roll_seconds / 5) * 5 = actual pre-roll duration`

To get shorter pre-roll, you would need to reduce `segment_duration` in the buffer configuration, but this increases overhead and may affect video quality.

### Minimum Video Duration

With default settings (3s pre-roll + 3s cooldown), the minimum video duration depends on:
- Pre-roll: 5 seconds (one 5-second segment)
- Cooldown: 3 seconds
- **Minimum total: 8+ seconds** (5s pre-roll + active motion + 3s cooldown)

## Raspberry Pi Setup

### RAM Disk (Automatic)

On Raspberry Pi, Device Pilot automatically creates and mounts a 200MB RAM disk at `/mnt/ramdisk` for the HLS buffer. This reduces SD card wear since the buffer constantly writes and deletes 5-second video segments.

The RAM disk is **not persistent** across reboots - it's created fresh each time you run Device Pilot. This requires `sudo` permissions for the mount operation.

If RAM disk creation fails (e.g., no sudo access), it falls back to `/tmp/device-pilot/buffer`.

To manually unmount the RAM disk after stopping Device Pilot:
```bash
sudo umount /mnt/ramdisk
```

### Storage Locations

| Data | Location |
|------|----------|
| HLS buffer | `/mnt/ramdisk/device-pilot/buffer` (RAM) |
| Session clips | `~/device-pilot/sessions` |
| Final recordings | `~/device-pilot-recordings` |

## Camera Setup

Configure your camera's GOP (keyframe interval) to match the segment duration (5 seconds = 150 frames at 30fps). This ensures each HLS segment starts with an I-frame for clean concatenation.

### Tested Cameras

#### Reolink FE-W (Fisheye)

| Stream | Resolution | Protocol |
|--------|------------|----------|
| Main (high-res) | 2560x2560 | rtsp:// |
| Sub (low-res) | 1024x1024 | rtsp:// |

```env
RTSP_URL_MAIN=rtsp://user:pass@192.168.x.x:554/h264Preview_01_main
RTSP_URL_SUB=rtsp://user:pass@192.168.x.x:554/h264Preview_01_sub
```

#### Ubiquiti Doorbell Lite

| Stream | Resolution | Protocol |
|--------|------------|----------|
| High | 1920x2560 | rtsps:// |
| Medium | 672x896 | rtsps:// |
| Low | 336x448 | rtsps:// |

Ubiquiti uses RTSPS (RTSP over TLS with SRTP). Stream URLs are available from the UniFi Protect web interface under camera settings.

```env
# Use high-res for recording, low-res for detection
RTSP_URL_MAIN=rtsps://192.168.x.x:7441/<high-stream-token>?enableSrtp
RTSP_URL_SUB=rtsps://192.168.x.x:7441/<low-stream-token>?enableSrtp
```

**Note:** Both `rtsp://` and `rtsps://` protocols are supported.

## License

MIT
