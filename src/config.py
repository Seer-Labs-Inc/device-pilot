"""Configuration management for Device Pilot."""

import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv


def _get_default_buffer_dir() -> Path:
    """Get platform-specific default buffer directory."""
    if sys.platform == "darwin":
        # Mac: use system temp with dedicated subfolder
        base = Path(tempfile.gettempdir()) / "device-pilot"
        return base / "buffer"
    else:
        # Linux/Raspberry Pi - use RAM disk if available
        ramdisk = Path("/mnt/ramdisk")
        if ramdisk.exists():
            return ramdisk / "device-pilot" / "buffer"
        base = Path(tempfile.gettempdir()) / "device-pilot"
        return base / "buffer"


def _get_default_sessions_dir() -> Path:
    """Get platform-specific default sessions directory."""
    if sys.platform == "darwin":
        # Mac: use system temp with dedicated subfolder
        base = Path(tempfile.gettempdir()) / "device-pilot"
        return base / "sessions"
    else:
        # Linux/Raspberry Pi - use home directory
        return Path.home() / "device-pilot" / "sessions"


def _get_default_evidence_dir() -> Path:
    """Get default evidence (recordings) directory."""
    # Always use a dedicated folder in user's home directory
    return Path.home() / "device-pilot-recordings"


@dataclass
class PilotConfig:
    """Configuration for the pilot system."""

    # Timing
    pre_roll_seconds: float = 3.0
    cooldown_seconds: float = 3.0
    segment_duration: float = 5.0

    # Detection thresholds
    motion_threshold: float = 0.02
    light_jump_threshold: float = 30.0

    # Paths
    buffer_dir: Path = field(default_factory=_get_default_buffer_dir)
    sessions_dir: Path = field(default_factory=_get_default_sessions_dir)
    evidence_dir: Path = field(default_factory=_get_default_evidence_dir)

    # Stream URLs
    rtsp_url_main: str = ""
    rtsp_url_sub: str = ""

    # Runtime
    verbose: bool = False

    def __post_init__(self):
        """Ensure paths are Path objects and directories exist."""
        self.buffer_dir = Path(self.buffer_dir)
        self.sessions_dir = Path(self.sessions_dir)
        self.evidence_dir = Path(self.evidence_dir)

    def ensure_directories(self):
        """Create necessary directories."""
        self.buffer_dir.mkdir(parents=True, exist_ok=True)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env_file: Optional[Path] = None) -> "PilotConfig":
        """Load configuration from environment variables."""
        if env_file:
            load_dotenv(env_file)
        else:
            load_dotenv()

        config = cls()

        # Timing
        if val := os.getenv("PILOT_PRE_ROLL_SECONDS"):
            config.pre_roll_seconds = float(val)
        if val := os.getenv("PILOT_COOLDOWN_SECONDS"):
            config.cooldown_seconds = float(val)
        if val := os.getenv("PILOT_SEGMENT_DURATION"):
            config.segment_duration = float(val)

        # Detection thresholds
        if val := os.getenv("PILOT_MOTION_THRESHOLD"):
            config.motion_threshold = float(val)
        if val := os.getenv("PILOT_LIGHT_JUMP_THRESHOLD"):
            config.light_jump_threshold = float(val)

        # Paths
        if val := os.getenv("PILOT_BUFFER_DIR"):
            config.buffer_dir = Path(val)
        if val := os.getenv("PILOT_SESSIONS_DIR"):
            config.sessions_dir = Path(val)
        if val := os.getenv("PILOT_EVIDENCE_DIR"):
            config.evidence_dir = Path(val)

        # Stream URLs
        if val := os.getenv("RTSP_URL_MAIN"):
            config.rtsp_url_main = val
        if val := os.getenv("RTSP_URL_SUB"):
            config.rtsp_url_sub = val

        return config
