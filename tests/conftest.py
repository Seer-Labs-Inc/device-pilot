"""Pytest configuration and fixtures."""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Generator

import numpy as np
import pytest

from src.config import PilotConfig
from src.session_manager import SessionManagerConfig


# Skip markers for tools that may not be available
def _check_command(cmd: str) -> bool:
    """Check if a command is available."""
    return shutil.which(cmd) is not None


requires_ffmpeg = pytest.mark.skipif(
    not _check_command("ffmpeg"),
    reason="FFmpeg not available",
)

requires_ffprobe = pytest.mark.skipif(
    not _check_command("ffprobe"),
    reason="FFprobe not available",
)

requires_fswatch = pytest.mark.skipif(
    not _check_command("fswatch"),
    reason="fswatch not available",
)


@pytest.fixture
def test_config(tmp_path: Path) -> PilotConfig:
    """Create a test configuration with temp directories."""
    return PilotConfig(
        pre_roll_seconds=5.0,
        cooldown_seconds=3.0,
        segment_duration=2.0,
        motion_threshold=0.02,
        light_jump_threshold=30.0,
        buffer_dir=tmp_path / "buffer",
        sessions_dir=tmp_path / "sessions",
        evidence_dir=tmp_path / "evidence",
        rtsp_url_main="rtsp://test/main",
        rtsp_url_sub="rtsp://test/sub",
    )


@pytest.fixture
def session_manager_config() -> SessionManagerConfig:
    """Create a session manager config for testing."""
    return SessionManagerConfig(
        pre_roll_seconds=5.0,
        cooldown_seconds=3.0,
    )


@pytest.fixture
def sample_frame() -> np.ndarray:
    """Create a sample video frame for testing."""
    # Create a simple 640x480 BGR frame
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :, 0] = 50   # Blue channel
    frame[:, :, 1] = 100  # Green channel
    frame[:, :, 2] = 150  # Red channel
    return frame


@pytest.fixture
def motion_frame() -> np.ndarray:
    """Create a frame with motion (white rectangle)."""
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    # Add a white rectangle that should trigger motion detection
    frame[100:300, 200:400, :] = 255
    return frame


@pytest.fixture
def bright_frame() -> np.ndarray:
    """Create a bright frame for light detection testing."""
    frame = np.ones((480, 640, 3), dtype=np.uint8) * 200
    return frame


@pytest.fixture
def dark_frame() -> np.ndarray:
    """Create a dark frame for light detection testing."""
    frame = np.ones((480, 640, 3), dtype=np.uint8) * 30
    return frame


@pytest.fixture
def hls_buffer(tmp_path: Path) -> Generator[Path, None, None]:
    """Generate a test HLS buffer with video segments."""
    if not _check_command("ffmpeg"):
        pytest.skip("FFmpeg not available")
    buffer_dir = tmp_path / "buffer"
    buffer_dir.mkdir()

    # Generate 30 seconds of test video split into 5-second segments
    # Using testsrc with 30fps
    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", "testsrc=duration=30:size=640x360:rate=30",
        "-c:v", "libx264",
        "-g", "150",  # GOP = 5 seconds at 30fps
        "-keyint_min", "150",
        "-sc_threshold", "0",
        "-f", "hls",
        "-hls_time", "5",
        "-hls_list_size", "0",
        "-hls_segment_filename", str(buffer_dir / "clip_%04d.ts"),
        str(buffer_dir / "stream.m3u8"),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        pytest.skip(f"Failed to generate test HLS: {result.stderr.decode()}")

    yield buffer_dir

    # Cleanup is handled by tmp_path fixture


@pytest.fixture
def test_video(tmp_path: Path) -> Generator[Path, None, None]:
    """Generate a simple test video file."""
    if not _check_command("ffmpeg"):
        pytest.skip("FFmpeg not available")
    video_path = tmp_path / "test_video.mp4"

    cmd = [
        "ffmpeg",
        "-f", "lavfi",
        "-i", "testsrc=duration=10:size=640x360:rate=30",
        "-c:v", "libx264",
        "-g", "150",
        "-keyint_min", "150",
        str(video_path),
    ]

    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        pytest.skip(f"Failed to generate test video: {result.stderr.decode()}")

    yield video_path
