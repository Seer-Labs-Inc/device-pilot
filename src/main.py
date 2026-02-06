"""Main entry point for Device Pilot."""

import argparse
import atexit
import logging
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Optional

from .buffer import HLSBuffer
from .config import PilotConfig
from .detector import Detector, RTSPCapture
from .platform import Platform
from .recorder import RecorderManager
from .session import Session
from .session_manager import SessionManager, SessionManagerConfig

logger = logging.getLogger(__name__)

# Global reference for atexit cleanup
_pilot_instance: Optional["PilotSystem"] = None


class PilotSystem:
    """Main system orchestrating all components."""

    def __init__(self, config: PilotConfig):
        global _pilot_instance
        self.config = config
        self.platform = Platform.get_current()

        # Components
        self.buffer: Optional[HLSBuffer] = None
        self.detector: Optional[Detector] = None
        self.capture: Optional[RTSPCapture] = None
        self.recorder_manager: Optional[RecorderManager] = None
        self.session_manager: Optional[SessionManager] = None

        self._running = False
        self._stopped = False  # Track if cleanup has been done
        self._setup_signal_handlers()

        # Register for atexit cleanup as a fallback
        _pilot_instance = self
        atexit.register(_atexit_cleanup)

    def _setup_signal_handlers(self):
        """Set up signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, signum, frame):
        """Handle shutdown signals."""
        logger.info(f"Received signal {signum}, shutting down...")
        self._running = False
        # Directly call stop for immediate cleanup (don't wait for main loop)
        self.stop()

    def _on_session_start(self, session: Session):
        """Handle new session start."""
        logger.info(f"Starting session {session.id}")

        # Get pre-roll clips from buffer
        preroll_clips = []
        if self.buffer:
            preroll_clips = self.buffer.get_preroll_clips(self.config.pre_roll_seconds)

        # Start recording
        if self.recorder_manager:
            self.recorder_manager.start_session(session.id, preroll_clips)

    def _on_session_finalize(self, session: Session):
        """Handle session finalization."""
        logger.info(f"Finalizing session {session.id}")

        if self.recorder_manager:
            output_path = self.recorder_manager.finalize_session(session.id)
            if output_path:
                logger.info(f"Session {session.id} saved to {output_path}")
            else:
                logger.error(f"Session {session.id} finalization failed")

    def _clear_old_sessions(self):
        """
        Clear old session data from previous runs.

        NOTE: This only clears the temp sessions directory (/tmp/device-pilot/sessions),
        NOT the recordings folder (~/device-pilot-recordings) which is preserved.
        """
        sessions_dir = self.config.sessions_dir
        if not sessions_dir.exists():
            return

        # Safety check: never clear the evidence/recordings directory
        if sessions_dir == self.config.evidence_dir:
            logger.error("Safety check: refusing to clear evidence directory")
            return

        cleared = 0
        for item in sessions_dir.iterdir():
            if item.is_dir():
                try:
                    shutil.rmtree(item)
                    cleared += 1
                except OSError as e:
                    logger.warning(f"Failed to remove old session {item}: {e}")
        if cleared > 0:
            logger.info(f"Cleared {cleared} old sessions from previous runs")

    def start(self) -> bool:
        """Start all system components."""
        logger.info("Starting Device Pilot...")

        # Ensure directories exist
        self.config.ensure_directories()

        # Set up buffer directory
        self.platform.setup_buffer_directory(self.config.buffer_dir)

        # Clear old session data from previous runs
        self._clear_old_sessions()

        # Initialize components
        self.detector = Detector(
            motion_threshold=self.config.motion_threshold,
            light_jump_threshold=self.config.light_jump_threshold,
        )

        self.buffer = HLSBuffer(
            rtsp_url=self.config.rtsp_url_main,
            buffer_dir=self.config.buffer_dir,
            segment_duration=self.config.segment_duration,
        )

        self.recorder_manager = RecorderManager(
            buffer_dir=self.config.buffer_dir,
            sessions_dir=self.config.sessions_dir,
            evidence_dir=self.config.evidence_dir,
            platform=self.platform,
        )

        session_config = SessionManagerConfig(
            pre_roll_seconds=self.config.pre_roll_seconds,
            cooldown_seconds=self.config.cooldown_seconds,
        )

        self.session_manager = SessionManager(
            config=session_config,
            on_session_start=self._on_session_start,
            on_session_finalize=self._on_session_finalize,
        )

        # Start HLS buffer
        if not self.buffer.start():
            logger.error("Failed to start HLS buffer")
            return False

        # Start buffer watcher for draining clips to active sessions
        self.recorder_manager.start_buffer_watcher()

        # Open detection stream
        self.capture = RTSPCapture(self.config.rtsp_url_sub)
        if not self.capture.open():
            logger.error("Failed to open detection stream")
            return False

        logger.info("Device Pilot started successfully")
        self._running = True
        return True

    def _reconnect_capture(self) -> bool:
        """
        Attempt to reconnect the detection stream with exponential backoff.

        Handles outages up to 5+ minutes by retrying indefinitely with
        exponential backoff (1s → 2s → 4s → ... capped at max_reconnect_delay).

        Returns True if reconnection succeeded, False if we should stop.
        """
        delay = 1.0
        max_delay = self.config.max_reconnect_delay
        disconnect_start = time.time()
        buffer_restarted = False

        while self._running:
            logger.warning(f"Attempting to reconnect in {delay:.0f}s...")
            time.sleep(delay)

            if not self._running:
                return False

            self.capture.release()
            if self.capture.open():
                disconnect_duration = time.time() - disconnect_start
                logger.info(f"Reconnected to detection stream after {disconnect_duration:.0f}s")
                # Reset detector state after reconnection to avoid false triggers
                self.detector.reset()
                return True

            # Log how long we've been disconnected
            disconnect_duration = time.time() - disconnect_start
            logger.error(f"Reconnection failed after {disconnect_duration:.0f}s, next attempt in {delay:.0f}s")

            # After 2 minutes of failures, restart the HLS buffer too (it's likely also affected)
            if disconnect_duration > 120 and not buffer_restarted:
                logger.warning("Extended outage detected, restarting HLS buffer...")
                self._restart_buffer()
                buffer_restarted = True

            # Exponential backoff with cap
            delay = min(delay * 2, max_delay)

        return False

    def _restart_buffer(self) -> bool:
        """Restart the HLS buffer (for recovery from network issues)."""
        logger.info("Restarting HLS buffer...")
        if self.buffer:
            self.buffer.stop()
            time.sleep(2)
            if self.buffer.start():
                logger.info("HLS buffer restarted successfully")
                return True
            logger.error("Failed to restart HLS buffer")
        return False

    def run(self):
        """Run the main detection loop."""
        if not self._running:
            if not self.start():
                return

        last_tick = time.time()
        start_time = time.time()
        motion_state = False
        motion_start_time: Optional[float] = None  # Track when motion started
        session_triggered = False  # Track if we've triggered session for current motion
        detection_enabled = False
        consecutive_failures = 0
        max_consecutive_failures = 10  # Restart buffer after this many failures

        try:
            while self._running:
                try:
                    current_time = time.time()

                    # Startup delay - wait before enabling detection
                    if not detection_enabled:
                        elapsed = current_time - start_time
                        if elapsed < self.config.startup_delay_seconds:
                            # Still in startup delay, just read frames to warm up
                            ret, _ = self.capture.read()
                            if not ret:
                                time.sleep(0.1)
                            continue
                        else:
                            detection_enabled = True
                            logger.info("Detection enabled after startup delay")

                    # Read frame from detection stream
                    ret, frame = self.capture.read()
                    if not ret:
                        consecutive_failures += 1
                        logger.warning(f"Failed to read frame ({consecutive_failures}/{max_consecutive_failures})")

                        # After too many failures, try restarting the buffer too
                        if consecutive_failures >= max_consecutive_failures:
                            logger.error("Too many consecutive failures, restarting buffer...")
                            self._restart_buffer()
                            consecutive_failures = 0

                        if not self._reconnect_capture():
                            break
                        continue

                    # Reset failure counter on successful read
                    consecutive_failures = 0

                    # Analyze frame
                    result = self.detector.analyze_frame(frame)

                    # Handle detection state changes with minimum motion duration
                    if result.motion_detected or result.light_event_detected:
                        if motion_start_time is None:
                            # Motion just started
                            motion_start_time = current_time
                            logger.debug(
                                f"Motion started (raw={result.motion_score:.3f}, "
                                f"smoothed={result.smoothed_motion_score:.3f}, "
                                f"light_delta={result.brightness_delta:.1f})"
                            )

                        # Check if motion has been sustained long enough
                        motion_duration = current_time - motion_start_time
                        if motion_duration >= self.config.min_motion_seconds:
                            if not motion_state:
                                logger.info(
                                    f"Motion confirmed after {motion_duration:.1f}s "
                                    f"(raw={result.motion_score:.3f}, "
                                    f"smoothed={result.smoothed_motion_score:.3f}, "
                                    f"light_delta={result.brightness_delta:.1f})"
                                )
                                motion_state = True
                            self.session_manager.on_motion_detected(current_time)
                    else:
                        # No motion detected
                        if motion_start_time is not None:
                            motion_duration = current_time - motion_start_time
                            if motion_state:
                                logger.info(f"Motion ended (duration: {motion_duration:.1f}s)")
                            elif motion_duration > 0.1:
                                logger.debug(f"Brief motion ignored ({motion_duration:.2f}s < {self.config.min_motion_seconds}s)")
                        motion_start_time = None
                        motion_state = False
                        self.session_manager.on_no_motion(current_time)

                    # Tick session manager periodically
                    if current_time - last_tick >= 1.0:
                        self.session_manager.tick(current_time)
                        last_tick = current_time

                    # Small delay to control CPU usage
                    time.sleep(0.033)  # ~30 FPS

                except Exception as e:
                    logger.error(f"Error in detection loop: {e}")
                    time.sleep(1)
        finally:
            # Ensure cleanup happens even on unexpected exit
            self.stop()

    def stop(self):
        """Stop all system components."""
        # Prevent double cleanup
        if self._stopped:
            return
        self._stopped = True

        logger.info("Stopping Device Pilot...")
        self._running = False

        # Finalize any active sessions
        if self.session_manager:
            for session in list(self.session_manager.active_sessions.values()):
                logger.info(f"Finalizing remaining session {session.id}")
                self._on_session_finalize(session)

        # Stop components - order matters: watcher first, then buffer
        if self.recorder_manager:
            self.recorder_manager.cleanup()

        if self.capture:
            self.capture.release()

        if self.buffer:
            self.buffer.stop()

        # Note: We do NOT clean up buffer_dir or sessions_dir here
        # to preserve evidence in case of crash

        logger.info("Device Pilot stopped")


def _atexit_cleanup():
    """Cleanup handler called on interpreter exit."""
    global _pilot_instance
    if _pilot_instance is not None:
        _pilot_instance.stop()


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Device Pilot - Motion/event detection and video capture"
    )

    parser.add_argument(
        "--pre-roll",
        type=float,
        help="Pre-roll duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--cooldown",
        type=float,
        help="Cooldown duration in seconds (default: 10)",
    )
    parser.add_argument(
        "--motion-threshold",
        type=float,
        help="Motion threshold 0-1 (default: 0.02)",
    )
    parser.add_argument(
        "--light-threshold",
        type=float,
        help="Light jump threshold 0-255 (default: 30)",
    )
    parser.add_argument(
        "--buffer-dir",
        type=Path,
        help="HLS buffer directory",
    )
    parser.add_argument(
        "--sessions-dir",
        type=Path,
        help="Sessions directory",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        help="Evidence output directory",
    )
    parser.add_argument(
        "--rtsp-main",
        type=str,
        help="Main RTSP stream URL (high-res for recording)",
    )
    parser.add_argument(
        "--rtsp-sub",
        type=str,
        help="Sub RTSP stream URL (low-res for detection)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )

    args = parser.parse_args()

    # Set up logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Load config from environment
    config = PilotConfig.from_env()

    # Override with CLI arguments
    if args.pre_roll is not None:
        config.pre_roll_seconds = args.pre_roll
    if args.cooldown is not None:
        config.cooldown_seconds = args.cooldown
    if args.motion_threshold is not None:
        config.motion_threshold = args.motion_threshold
    if args.light_threshold is not None:
        config.light_jump_threshold = args.light_threshold
    if args.buffer_dir is not None:
        config.buffer_dir = args.buffer_dir
    if args.sessions_dir is not None:
        config.sessions_dir = args.sessions_dir
    if args.evidence_dir is not None:
        config.evidence_dir = args.evidence_dir
    if args.rtsp_main is not None:
        config.rtsp_url_main = args.rtsp_main
    if args.rtsp_sub is not None:
        config.rtsp_url_sub = args.rtsp_sub
    config.verbose = args.verbose

    # Validate required settings
    if not config.rtsp_url_main or not config.rtsp_url_sub:
        logger.error("RTSP URLs required. Set RTSP_URL_MAIN and RTSP_URL_SUB in .env or via CLI")
        sys.exit(1)

    # Run the system
    pilot = PilotSystem(config)
    pilot.run()


if __name__ == "__main__":
    main()
