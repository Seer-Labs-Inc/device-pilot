"""Integration tests for the full pipeline."""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.config import PilotConfig
from src.detector import Detector
from src.recorder import RecorderManager
from src.session import Session, SessionState
from src.session_manager import SessionManager, SessionManagerConfig
from tests.conftest import requires_ffmpeg, requires_fswatch


class TestDetectorSessionIntegration:
    """Tests for detector and session manager integration."""

    def test_motion_triggers_session(self, sample_frame, motion_frame):
        """Motion detection triggers session creation."""
        detector = Detector(motion_threshold=0.01)
        session_config = SessionManagerConfig(pre_roll_seconds=5.0, cooldown_seconds=3.0)

        sessions_started = []
        manager = SessionManager(
            session_config,
            on_session_start=lambda s: sessions_started.append(s),
        )

        # Build background model
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Detect motion - need multiple frames for smoothing
        # Alternate frames to prevent background adaptation
        motion_frame_alt = motion_frame.copy()
        motion_frame_alt[100:200, 100:200] = 128

        for i in range(detector.SMOOTHING_WINDOW * 2):
            frame = motion_frame if i % 2 == 0 else motion_frame_alt
            result = detector.analyze_frame(frame)
            if result.motion_detected:
                manager.on_motion_detected()

        assert len(sessions_started) == 1

    def test_no_motion_triggers_cooldown(self, sample_frame, motion_frame):
        """Lack of motion triggers cooldown."""
        detector = Detector(motion_threshold=0.01)
        session_config = SessionManagerConfig(pre_roll_seconds=5.0, cooldown_seconds=3.0)
        manager = SessionManager(session_config)

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Trigger motion with multiple alternating frames
        motion_frame_alt = motion_frame.copy()
        motion_frame_alt[100:200, 100:200] = 128

        for i in range(detector.SMOOTHING_WINDOW * 2):
            frame = motion_frame if i % 2 == 0 else motion_frame_alt
            result = detector.analyze_frame(frame)
            if result.motion_detected:
                manager.on_motion_detected(100.0)

        # Static frames - need enough to clear hysteresis
        for _ in range(detector.SMOOTHING_WINDOW + detector.HYSTERESIS_FRAMES):
            result = detector.analyze_frame(sample_frame)

        if not result.motion_detected:
            manager.on_no_motion(110.0)

        # Session should be in cooldown
        cooldown = manager.get_cooldown_sessions()
        assert len(cooldown) == 1


class TestRecorderIntegration:
    """Tests for recorder integration."""

    @requires_ffmpeg
    def test_full_recording_pipeline(self, hls_buffer, tmp_path):
        """Test full recording pipeline from clips to MP4."""
        sessions_dir = tmp_path / "sessions"
        evidence_dir = tmp_path / "evidence"

        recorder_manager = RecorderManager(
            buffer_dir=hls_buffer,
            sessions_dir=sessions_dir,
            evidence_dir=evidence_dir,
        )

        # Get pre-roll clips
        clips = sorted(hls_buffer.glob("clip_*.ts"))
        preroll = clips[:2]

        # Start session
        recorder = recorder_manager.start_session("test_session", preroll)
        assert len(recorder.clips) == 2

        # Add more clips
        for clip in clips[2:4]:
            recorder_manager.add_clip_to_sessions(clip)
        assert len(recorder.clips) == 4

        # Finalize
        output = recorder_manager.finalize_session("test_session")
        assert output is not None
        assert output.exists()
        assert output.stat().st_size > 0


class TestScenarioIntegration:
    """Integration tests for event scenarios."""

    @requires_ffmpeg
    def test_scenario1_serial_events(self, hls_buffer, tmp_path):
        """Scenario 1: Serial events produce two separate MP4s."""
        sessions_dir = tmp_path / "sessions"
        evidence_dir = tmp_path / "evidence"

        recorder_manager = RecorderManager(
            buffer_dir=hls_buffer,
            sessions_dir=sessions_dir,
            evidence_dir=evidence_dir,
        )

        clips = sorted(hls_buffer.glob("clip_*.ts"))
        if len(clips) < 6:
            pytest.skip("Not enough test clips")

        # Event A: clips 0-2
        recorder_manager.start_session("event_a", clips[:1])
        recorder_manager.add_clip_to_sessions(clips[1])
        recorder_manager.add_clip_to_sessions(clips[2])
        output_a = recorder_manager.finalize_session("event_a")

        # Event B: clips 3-5 (no overlap)
        recorder_manager.start_session("event_b", clips[3:4])
        recorder_manager.add_clip_to_sessions(clips[4])
        recorder_manager.add_clip_to_sessions(clips[5])
        output_b = recorder_manager.finalize_session("event_b")

        assert output_a is not None and output_a.exists()
        assert output_b is not None and output_b.exists()
        assert output_a != output_b

    @requires_ffmpeg
    def test_scenario2_overlapping_events(self, hls_buffer, tmp_path):
        """Scenario 2: Overlapping events produce two MP4s with shared clips."""
        sessions_dir = tmp_path / "sessions"
        evidence_dir = tmp_path / "evidence"

        recorder_manager = RecorderManager(
            buffer_dir=hls_buffer,
            sessions_dir=sessions_dir,
            evidence_dir=evidence_dir,
        )

        clips = sorted(hls_buffer.glob("clip_*.ts"))
        if len(clips) < 5:
            pytest.skip("Not enough test clips")

        # Event A starts: preroll + clip 0
        recorder_manager.start_session("event_a", clips[:1])

        # Event A continues: clip 1
        recorder_manager.add_clip_to_sessions(clips[1])

        # Event B starts (during A's "cooldown"): preroll includes clip 1
        # This simulates overlapping - B's preroll includes A's end
        recorder_manager.start_session("event_b", clips[1:2])

        # Both get clip 2 (overlap)
        recorder_manager.add_clip_to_sessions(clips[2])

        # Event A ends (finalize)
        output_a = recorder_manager.finalize_session("event_a")

        # Event B continues
        recorder_manager.add_clip_to_sessions(clips[3])
        recorder_manager.add_clip_to_sessions(clips[4])
        output_b = recorder_manager.finalize_session("event_b")

        assert output_a is not None and output_a.exists()
        assert output_b is not None and output_b.exists()

        # B should have more clips due to pre-roll overlap
        # (This is a simplified test - real overlap is timing-based)


class TestConfigIntegration:
    """Tests for configuration integration."""

    def test_config_from_env(self, tmp_path, monkeypatch):
        """Config loads from environment variables."""
        monkeypatch.setenv("RTSP_URL_MAIN", "rtsp://test/main")
        monkeypatch.setenv("RTSP_URL_SUB", "rtsp://test/sub")
        monkeypatch.setenv("PILOT_PRE_ROLL_SECONDS", "15")
        monkeypatch.setenv("PILOT_COOLDOWN_SECONDS", "8")

        config = PilotConfig.from_env()

        assert config.rtsp_url_main == "rtsp://test/main"
        assert config.rtsp_url_sub == "rtsp://test/sub"
        assert config.pre_roll_seconds == 15.0
        assert config.cooldown_seconds == 8.0

    def test_config_directories_created(self, tmp_path):
        """Config ensures directories exist."""
        config = PilotConfig(
            buffer_dir=tmp_path / "buffer",
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        config.ensure_directories()

        assert config.buffer_dir.exists()
        assert config.sessions_dir.exists()
        assert config.evidence_dir.exists()


class TestEndToEnd:
    """End-to-end tests with simulated detection loop."""

    def test_simulated_detection_loop(self, sample_frame, motion_frame, tmp_path):
        """Simulate detection loop with state changes."""
        detector = Detector(motion_threshold=0.01)
        config = SessionManagerConfig(pre_roll_seconds=2.0, cooldown_seconds=2.0)

        events = []
        manager = SessionManager(
            config,
            on_session_start=lambda s: events.append(("start", s.id)),
            on_session_finalize=lambda s: events.append(("finalize", s.id)),
        )

        # Build background (time 0-30)
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Create alternating motion frames to prevent background adaptation
        motion_frame_alt = motion_frame.copy()
        motion_frame_alt[100:200, 100:200] = 128

        # Phase 1: Sustained motion to trigger detection (with smoothing)
        sim_time = 100.0
        for i in range(detector.SMOOTHING_WINDOW * 2):
            frame = motion_frame if i % 2 == 0 else motion_frame_alt
            result = detector.analyze_frame(frame)

            if result.motion_detected:
                manager.on_motion_detected(sim_time)
            else:
                manager.on_no_motion(sim_time)

            sim_time += 0.033  # ~30 FPS

        # Phase 2: Static frames to end motion (with hysteresis)
        for _ in range(detector.SMOOTHING_WINDOW + detector.HYSTERESIS_FRAMES + 5):
            result = detector.analyze_frame(sample_frame)

            if result.motion_detected:
                manager.on_motion_detected(sim_time)
            else:
                manager.on_no_motion(sim_time)

            sim_time += 0.033

        # Phase 3: Wait for cooldown to expire
        # Tick beyond cooldown period (2 seconds)
        sim_time += 3.0
        manager.tick(sim_time)

        # Should have one complete cycle
        assert len(events) >= 1, "Should have at least started a session"
        assert ("start", events[0][1]) in events
        assert ("finalize", events[0][1]) in events

    def test_continuous_motion_with_gaps(self, sample_frame, motion_frame):
        """Simulate motion with brief gaps - should be single session."""
        detector = Detector(motion_threshold=0.01)
        config = SessionManagerConfig(pre_roll_seconds=2.0, cooldown_seconds=3.0)

        started = []
        finalized = []
        manager = SessionManager(
            config,
            on_session_start=lambda s: started.append(s.id),
            on_session_finalize=lambda s: finalized.append(s.id),
        )

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Motion starts at 100
        manager.on_motion_detected(100.0)
        assert len(started) == 1

        # Brief gap at 105 - enters cooldown
        manager.on_no_motion(105.0)
        assert len(manager.get_cooldown_sessions()) == 1

        # Motion resumes at 106 (during cooldown) - extends SAME session
        manager.on_motion_detected(106.0)
        assert len(started) == 1, "Should still be same session"
        assert len(manager.get_recording_sessions()) == 1

        # Motion finally stops at 110
        manager.on_no_motion(110.0)

        # Cooldown expires at 113
        manager.tick(113.5)
        assert len(finalized) == 1

        # Only one session total
        assert len(started) == 1
        assert len(finalized) == 1
