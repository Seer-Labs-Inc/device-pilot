"""HLS buffer management using FFmpeg."""

import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ClipInfo:
    """Information about an HLS clip."""

    path: Path
    index: int
    timestamp: float  # File modification time


class HLSBuffer:
    """
    Manages HLS buffer from RTSP stream using FFmpeg.

    FFmpeg captures the RTSP stream and outputs HLS segments (.ts files)
    to a buffer directory. This class manages that buffer and provides
    access to clips for pre-roll and recording.
    """

    # Alert if segment count exceeds max by this margin
    SEGMENT_OVERFLOW_MARGIN = 5

    def __init__(
        self,
        rtsp_url: str,
        buffer_dir: Path,
        segment_duration: float = 5.0,
        max_segments: int = 20,
    ):
        """
        Initialize the HLS buffer.

        Args:
            rtsp_url: RTSP stream URL
            buffer_dir: Directory for HLS segments
            segment_duration: Duration of each segment in seconds
            max_segments: Maximum number of segments to keep
        """
        self.rtsp_url = rtsp_url
        self.buffer_dir = buffer_dir
        self.segment_duration = segment_duration
        self.max_segments = max_segments

        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._running = False
        self._overflow_warned = False

    def _clear_old_clips(self):
        """
        Clear old clips and playlist from previous runs.

        NOTE: This only clears the temp buffer directory (/tmp/device-pilot/buffer),
        NOT the recordings folder (~/device-pilot-recordings) which is preserved.
        """
        cleared = 0
        for pattern in ["clip_*.ts", "stream.m3u8"]:
            for path in self.buffer_dir.glob(pattern):
                try:
                    path.unlink()
                    cleared += 1
                except OSError as e:
                    logger.warning(f"Failed to remove old file {path}: {e}")
        if cleared > 0:
            logger.info(f"Cleared {cleared} old files from buffer directory")

    def start(self) -> bool:
        """Start the FFmpeg HLS capture."""
        self.buffer_dir.mkdir(parents=True, exist_ok=True)

        # Clear old clips from previous runs to avoid mixing old footage
        self._clear_old_clips()

        # Build FFmpeg command based on stream type
        cmd = ["ffmpeg"]

        # Add protocol-specific options
        if self.rtsp_url.startswith("rtsps://"):
            # RTSPS (RTSP over TLS) - used by Ubiquiti cameras
            # Don't verify certificates (self-signed), use TCP for SRTP
            cmd.extend([
                "-rtsp_transport", "tcp",
            ])
        else:
            # Standard RTSP - use TCP transport
            cmd.extend([
                "-rtsp_transport", "tcp",
            ])

        cmd.extend([
            "-i", self.rtsp_url,
            "-c:v", "copy",
            "-c:a", "copy",
            "-f", "hls",
            "-hls_time", str(int(self.segment_duration)),
            "-hls_list_size", str(self.max_segments),
            "-hls_flags", "delete_segments",
            "-hls_segment_filename", str(self.buffer_dir / "clip_%04d.ts"),
            str(self.buffer_dir / "stream.m3u8"),
        ])

        try:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self._running = True

            # Start monitor thread to log FFmpeg errors
            self._monitor_thread = threading.Thread(target=self._monitor_ffmpeg, daemon=True)
            self._monitor_thread.start()

            # Wait a bit for FFmpeg to start
            time.sleep(2)

            return self._process.poll() is None

        except Exception as e:
            logger.error(f"Failed to start FFmpeg: {e}")
            return False

    def stop(self):
        """Stop the FFmpeg HLS capture."""
        self._running = False

        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)

    def _monitor_ffmpeg(self):
        """Monitor FFmpeg stderr for errors."""
        if not self._process:
            return

        while self._running and self._process.poll() is None:
            try:
                line = self._process.stderr.readline()
                if line:
                    decoded = line.decode().strip()
                    if "error" in decoded.lower():
                        logger.error(f"FFmpeg: {decoded}")
            except Exception:
                break

    def get_clips(self) -> List[ClipInfo]:
        """Get list of available clips sorted by index."""
        clips = []
        pattern = re.compile(r"clip_(\d+)\.ts")

        for path in self.buffer_dir.glob("clip_*.ts"):
            match = pattern.match(path.name)
            if match:
                clips.append(ClipInfo(
                    path=path,
                    index=int(match.group(1)),
                    timestamp=path.stat().st_mtime,
                ))

        sorted_clips = sorted(clips, key=lambda c: c.index)

        # Check for unexpected growth
        self._check_buffer_overflow(sorted_clips)

        return sorted_clips

    def _check_buffer_overflow(self, clips: List[ClipInfo]):
        """Check if buffer has grown beyond expected size and clean up if needed."""
        clip_count = len(clips)
        threshold = self.max_segments + self.SEGMENT_OVERFLOW_MARGIN

        if clip_count > threshold:
            if not self._overflow_warned:
                logger.warning(
                    f"Buffer overflow detected: {clip_count} segments "
                    f"(expected max {self.max_segments}). Cleaning up old segments."
                )
                self._overflow_warned = True

            # Remove oldest segments to get back to max_segments
            clips_to_remove = clip_count - self.max_segments
            for clip in clips[:clips_to_remove]:
                try:
                    clip.path.unlink()
                    logger.debug(f"Removed overflow segment: {clip.path.name}")
                except OSError as e:
                    logger.error(f"Failed to remove {clip.path}: {e}")
        elif clip_count <= self.max_segments:
            # Reset warning flag when back to normal
            self._overflow_warned = False

    def get_segment_count(self) -> int:
        """Get the current number of segments in the buffer."""
        return len(list(self.buffer_dir.glob("clip_*.ts")))

    def get_preroll_clips(self, seconds: float) -> List[Path]:
        """
        Get clips for pre-roll capture.

        Args:
            seconds: Number of seconds of pre-roll needed

        Returns:
            List of clip paths covering the requested duration
        """
        clips = self.get_clips()
        if not clips:
            return []

        # Calculate how many clips we need
        num_clips = int(seconds / self.segment_duration) + 1

        # Get the most recent clips
        preroll_clips = clips[-num_clips:] if len(clips) >= num_clips else clips

        return [c.path for c in preroll_clips]

    def get_latest_clip(self) -> Optional[Path]:
        """Get the most recent clip."""
        clips = self.get_clips()
        return clips[-1].path if clips else None

    @property
    def is_running(self) -> bool:
        """Check if FFmpeg is running."""
        return self._process is not None and self._process.poll() is None


def copy_clips(clips: List[Path], destination: Path) -> List[Path]:
    """
    Copy clips to a destination directory.

    Args:
        clips: List of clip paths to copy
        destination: Destination directory

    Returns:
        List of copied clip paths
    """
    destination.mkdir(parents=True, exist_ok=True)
    copied = []

    for clip in clips:
        dest_path = destination / clip.name
        shutil.copy2(clip, dest_path)
        copied.append(dest_path)

    return copied
