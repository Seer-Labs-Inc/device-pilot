"""Recording management - clip draining and MP4 concatenation."""

import logging
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .platform import Platform, WatcherHandle, safe_rmtree

logger = logging.getLogger(__name__)


@dataclass
class SessionRecorder:
    """Manages recording for a single session."""

    session_id: str
    session_dir: Path
    evidence_dir: Path
    start_time: float = field(default_factory=time.time)
    clips: List[Path] = field(default_factory=list)
    watcher: Optional[WatcherHandle] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_clip(self, clip_path: Path):
        """Add a clip to this session (thread-safe)."""
        with self._lock:
            # Copy to session directory
            dest = self.session_dir / clip_path.name
            if not dest.exists():
                shutil.copy2(clip_path, dest)
                self.clips.append(dest)
                logger.debug(f"Session {self.session_id}: Added clip {clip_path.name}")

    def finalize(self) -> Optional[Path]:
        """
        Finalize the session by concatenating clips to MP4.

        Returns:
            Path to the output MP4, or None if failed
        """
        if self.watcher:
            self.watcher.stop()
            self.watcher = None

        with self._lock:
            if not self.clips:
                logger.warning(f"Session {self.session_id}: No clips to finalize")
                return None

            # Sort clips by name to ensure correct order
            sorted_clips = sorted(self.clips, key=lambda p: p.name)

            # Create concat file
            concat_file = self.session_dir / "concat.txt"
            with open(concat_file, "w") as f:
                for clip in sorted_clips:
                    f.write(f"file '{clip}'\n")

            # Output path with timestamp for chronological sorting
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.fromtimestamp(self.start_time).strftime("%Y%m%d_%H%M%S")
            output_path = self.evidence_dir / f"event_{timestamp}_{self.session_id}.mp4"

            # Run FFmpeg concat
            cmd = [
                "ffmpeg",
                "-y",  # Overwrite
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_file),
                "-c", "copy",
                str(output_path),
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=60,
                )

                if result.returncode == 0:
                    logger.info(f"Session {self.session_id}: Created {output_path}")
                    return output_path
                else:
                    logger.error(
                        f"Session {self.session_id}: FFmpeg failed: {result.stderr.decode()}"
                    )
                    return None

            except subprocess.TimeoutExpired:
                logger.error(f"Session {self.session_id}: FFmpeg timeout")
                return None
            except Exception as e:
                logger.error(f"Session {self.session_id}: Finalize error: {e}")
                return None

    def cleanup(self):
        """Clean up session directory (only if safe)."""
        if self.watcher:
            self.watcher.stop()
        safe_rmtree(self.session_dir)


class RecorderManager:
    """Manages multiple session recorders."""

    # Polling interval for fallback clip detection
    POLL_INTERVAL = 1.0

    def __init__(
        self,
        buffer_dir: Path,
        sessions_dir: Path,
        evidence_dir: Path,
        platform: Optional[Platform] = None,
    ):
        """
        Initialize the recorder manager.

        Args:
            buffer_dir: Directory where HLS buffer clips are stored
            sessions_dir: Directory for session working files
            evidence_dir: Directory for output MP4 files
            platform: Platform implementation (auto-detected if None)
        """
        self.buffer_dir = buffer_dir
        self.sessions_dir = sessions_dir
        self.evidence_dir = evidence_dir
        self.platform = platform or Platform.get_current()

        self.recorders: Dict[str, SessionRecorder] = {}
        self._clip_callback: Optional[Callable[[Path], None]] = None
        self._buffer_watcher: Optional[WatcherHandle] = None
        self._seen_clips: set = set()  # Track clips we've already processed
        self._poll_thread: Optional[threading.Thread] = None
        self._polling = False

    def start_session(
        self,
        session_id: str,
        preroll_clips: List[Path],
    ) -> SessionRecorder:
        """
        Start recording for a new session.

        Args:
            session_id: Unique session identifier
            preroll_clips: Pre-roll clips to copy

        Returns:
            The session recorder
        """
        session_dir = self.sessions_dir / session_id
        session_dir.mkdir(parents=True, exist_ok=True)

        recorder = SessionRecorder(
            session_id=session_id,
            session_dir=session_dir,
            evidence_dir=self.evidence_dir,
        )

        # Copy pre-roll clips
        for clip in preroll_clips:
            recorder.add_clip(clip)

        self.recorders[session_id] = recorder
        logger.info(f"Started session {session_id} with {len(preroll_clips)} pre-roll clips")

        return recorder

    def add_clip_to_sessions(self, clip_path: Path):
        """Add a new clip to all active sessions."""
        for recorder in self.recorders.values():
            recorder.add_clip(clip_path)

    def _on_new_clip(self, path: Path):
        """Handle a new clip being detected (from watcher or polling)."""
        # Track that we've seen this clip
        clip_name = path.name
        if clip_name in self._seen_clips:
            return  # Already processed
        self._seen_clips.add(clip_name)

        # Add to all active sessions
        self.add_clip_to_sessions(path)

        # Call optional callback
        if self._clip_callback:
            self._clip_callback(path)

    def _poll_for_clips(self):
        """
        Polling fallback to detect new clips.

        This runs in a separate thread and periodically scans the buffer
        directory for new clips. This ensures clips are captured even if
        the inotifywait watcher fails.
        """
        logger.debug(f"Starting clip polling on {self.buffer_dir}")
        while self._polling:
            try:
                # Scan for .ts files
                for clip_path in self.buffer_dir.glob("clip_*.ts"):
                    if clip_path.name not in self._seen_clips:
                        # Verify file is complete (not being written)
                        try:
                            size1 = clip_path.stat().st_size
                            time.sleep(0.1)
                            size2 = clip_path.stat().st_size
                            if size1 == size2 and size1 > 0:
                                # File is stable, process it
                                self._on_new_clip(clip_path)
                        except OSError:
                            pass  # File may have been deleted
            except Exception as e:
                logger.error(f"Error in clip polling: {e}")

            time.sleep(self.POLL_INTERVAL)

    def start_buffer_watcher(self, callback: Optional[Callable[[Path], None]] = None):
        """
        Start watching the buffer directory for new clips.

        Uses both inotifywait (for immediate detection) and polling
        (as fallback for reliability).

        Args:
            callback: Optional callback for new clips (in addition to adding to sessions)
        """
        self._clip_callback = callback
        self._seen_clips.clear()

        # Start the file watcher (inotifywait on Linux, fswatch on Mac)
        self._buffer_watcher = self.platform.start_file_watcher(
            self.buffer_dir,
            self._on_new_clip,
            pattern="*.ts",
        )
        logger.debug(f"Started file watcher on {self.buffer_dir}")

        # Start polling fallback
        self._polling = True
        self._poll_thread = threading.Thread(target=self._poll_for_clips, daemon=True)
        self._poll_thread.start()

    def stop_buffer_watcher(self):
        """Stop watching the buffer directory."""
        # Stop polling first
        self._polling = False
        if self._poll_thread:
            self._poll_thread.join(timeout=2)
            self._poll_thread = None

        # Stop file watcher
        if self._buffer_watcher:
            self._buffer_watcher.stop()
            self._buffer_watcher = None

        logger.debug("Stopped buffer watcher")

    def finalize_session(self, session_id: str) -> Optional[Path]:
        """
        Finalize a session and create the MP4.

        Args:
            session_id: Session to finalize

        Returns:
            Path to the output MP4, or None if failed
        """
        recorder = self.recorders.get(session_id)
        if not recorder:
            logger.warning(f"Session {session_id} not found")
            return None

        output_path = recorder.finalize()

        # Keep session files for debugging? Or clean up?
        # For now, clean up on success
        if output_path:
            recorder.cleanup()
            del self.recorders[session_id]

        return output_path

    def cleanup(self):
        """Clean up all sessions and stop watchers."""
        self.stop_buffer_watcher()

        for recorder in list(self.recorders.values()):
            recorder.cleanup()

        self.recorders.clear()
