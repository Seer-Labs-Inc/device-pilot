"""Tests for session manager and multi-session scenarios."""

import pytest

from src.session import SessionState
from src.session_manager import SessionManager, SessionManagerConfig


class TestSessionManager:
    """Tests for SessionManager class."""

    def test_initial_state(self, session_manager_config):
        """Manager starts with no active sessions."""
        manager = SessionManager(session_manager_config)
        assert manager.get_active_session_count() == 0
        assert len(manager.completed_sessions) == 0

    def test_motion_starts_session(self, session_manager_config):
        """Motion detection starts a new session."""
        manager = SessionManager(session_manager_config)
        manager.on_motion_detected(100.0)

        assert manager.get_active_session_count() == 1
        sessions = manager.get_recording_sessions()
        assert len(sessions) == 1
        assert sessions[0].state == SessionState.RECORDING

    def test_no_motion_enters_cooldown(self, session_manager_config):
        """No motion transitions session to cooldown."""
        manager = SessionManager(session_manager_config)
        manager.on_motion_detected(100.0)
        manager.on_no_motion(110.0)

        sessions = manager.get_cooldown_sessions()
        assert len(sessions) == 1
        assert sessions[0].cooldown_start_time == 110.0

    def test_session_finalizes_after_cooldown(self, session_manager_config):
        """Session finalizes when cooldown expires."""
        finalized_sessions = []

        def on_finalize(session):
            finalized_sessions.append(session)

        manager = SessionManager(
            session_manager_config,
            on_session_finalize=on_finalize,
        )

        manager.on_motion_detected(100.0)
        manager.on_no_motion(110.0)

        # Tick before cooldown expires (3 seconds)
        manager.tick(112.0)
        assert len(finalized_sessions) == 0

        # Tick after cooldown expires
        manager.tick(114.0)
        assert len(finalized_sessions) == 1
        assert manager.get_active_session_count() == 0

    def test_motion_extends_recording(self, session_manager_config):
        """Continued motion extends recording session."""
        manager = SessionManager(session_manager_config)
        manager.on_motion_detected(100.0)
        manager.on_motion_detected(105.0)

        # Still just one session
        assert manager.get_active_session_count() == 1
        sessions = manager.get_recording_sessions()
        assert sessions[0].last_activity_time == 105.0

    def test_motion_during_cooldown_extends_session(self, session_manager_config):
        """Motion during cooldown extends the SAME session (exits cooldown)."""
        manager = SessionManager(session_manager_config)
        manager.on_motion_detected(100.0)
        manager.on_no_motion(110.0)

        # Session in cooldown
        assert len(manager.get_cooldown_sessions()) == 1

        # Motion resumes before cooldown expires - extends SAME session
        manager.on_motion_detected(111.0)

        # Still just 1 session, now back to recording
        assert manager.get_active_session_count() == 1
        assert len(manager.get_recording_sessions()) == 1
        assert len(manager.get_cooldown_sessions()) == 0


class TestScenario1Serial:
    """Tests for Scenario 1: Serial events with gap between them."""

    def test_serial_events_create_two_sessions(self, session_manager_config):
        """Two separate motion events create two separate sessions."""
        finalized_sessions = []

        def on_finalize(session):
            finalized_sessions.append(session)

        manager = SessionManager(
            session_manager_config,
            on_session_finalize=on_finalize,
        )

        # Event A
        manager.on_motion_detected(100.0)
        manager.on_no_motion(110.0)

        # Let A's cooldown expire (cooldown = 3s)
        manager.tick(114.0)
        assert len(finalized_sessions) == 1

        # Event B (after gap)
        manager.on_motion_detected(120.0)
        manager.on_no_motion(130.0)

        # Let B's cooldown expire
        manager.tick(134.0)
        assert len(finalized_sessions) == 2

        # Both sessions should be completed
        assert len(manager.completed_sessions) == 2

    def test_serial_events_no_overlap(self, session_manager_config):
        """Serial events should have no time overlap."""
        sessions_started = []
        sessions_finalized = []

        def on_start(session):
            sessions_started.append((session.id, session.start_time))

        def on_finalize(session):
            sessions_finalized.append((session.id, session.cooldown_start_time))

        manager = SessionManager(
            session_manager_config,
            on_session_start=on_start,
            on_session_finalize=on_finalize,
        )

        # Event A: 100-110
        manager.on_motion_detected(100.0)
        manager.on_no_motion(110.0)
        manager.tick(114.0)  # Finalize A

        # Event B: 120-130
        manager.on_motion_detected(120.0)
        manager.on_no_motion(130.0)
        manager.tick(134.0)  # Finalize B

        # Verify no overlap
        a_end = sessions_finalized[0][1] + session_manager_config.cooldown_seconds
        b_start = sessions_started[1][1] - session_manager_config.pre_roll_seconds

        assert b_start >= a_end, "Events should not overlap in serial scenario"


class TestScenario2Overlapping:
    """Tests for Scenario 2: Overlapping events (new event shortly after previous ends)."""

    def test_overlapping_preroll_after_session_ends(self, session_manager_config):
        """New session's pre-roll can overlap with previous session's footage."""
        started_sessions = []
        finalized_sessions = []

        def on_start(session):
            started_sessions.append({"id": session.id, "start_time": session.start_time})

        def on_finalize(session):
            finalized_sessions.append(session.id)

        manager = SessionManager(
            session_manager_config,
            on_session_start=on_start,
            on_session_finalize=on_finalize,
        )

        # Event A: 100-105, then cooldown
        manager.on_motion_detected(100.0)
        manager.on_no_motion(105.0)

        # A's cooldown expires at 108 (105 + 3s cooldown)
        manager.tick(108.5)
        assert len(finalized_sessions) == 1
        assert manager.get_active_session_count() == 0

        # Event B starts at 109 (shortly after A ended)
        # B's pre-roll (109 - 3 = 106) overlaps with A's cooldown period (105-108)
        manager.on_motion_detected(109.0)
        assert len(started_sessions) == 2

        # Verify overlap: B's pre-roll starts before A ended
        b_preroll_start = started_sessions[1]["start_time"] - session_manager_config.pre_roll_seconds
        a_end_time = 108.0  # 105 + 3s cooldown
        assert b_preroll_start < a_end_time, "B's pre-roll should overlap with A's end"

    def test_motion_during_cooldown_extends_not_overlaps(self, session_manager_config):
        """Motion during cooldown extends session, doesn't create overlap."""
        started_sessions = []

        def on_start(session):
            started_sessions.append(session.id)

        manager = SessionManager(
            session_manager_config,
            on_session_start=on_start,
        )

        # Event A starts
        manager.on_motion_detected(100.0)
        assert len(started_sessions) == 1

        # A enters cooldown
        manager.on_no_motion(105.0)

        # Motion resumes during cooldown - should extend A, not create B
        manager.on_motion_detected(106.0)
        assert len(started_sessions) == 1  # Still just A
        assert manager.get_active_session_count() == 1
        assert len(manager.get_recording_sessions()) == 1

    def test_consecutive_events_after_cooldown(self, session_manager_config):
        """Two separate events after cooldown expires create two sessions."""
        finalized_sessions = []

        def on_finalize(session):
            finalized_sessions.append(session.id)

        manager = SessionManager(
            session_manager_config,
            on_session_finalize=on_finalize,
        )

        # Event A
        manager.on_motion_detected(100.0)
        manager.on_no_motion(105.0)
        manager.tick(108.5)  # A finalizes
        assert len(finalized_sessions) == 1

        # Event B (after A ended)
        manager.on_motion_detected(110.0)
        manager.on_no_motion(115.0)
        manager.tick(118.5)  # B finalizes
        assert len(finalized_sessions) == 2


class TestEdgeCases:
    """Edge case tests for session manager."""

    def test_rapid_motion_toggles(self, session_manager_config):
        """Rapid motion on/off extends the same session (no fragmentation)."""
        manager = SessionManager(session_manager_config)

        # First motion starts a session
        manager.on_motion_detected(100.0)
        assert manager.get_active_session_count() == 1

        # While recording, more motion just extends the session
        manager.on_motion_detected(100.5)
        assert manager.get_active_session_count() == 1

        # Stop motion - enters cooldown
        manager.on_no_motion(101.0)
        assert len(manager.get_cooldown_sessions()) == 1

        # Motion during cooldown extends SAME session (back to recording)
        manager.on_motion_detected(101.5)
        assert manager.get_active_session_count() == 1
        assert len(manager.get_recording_sessions()) == 1

    def test_no_motion_without_motion(self, session_manager_config):
        """No motion event without prior motion does nothing."""
        manager = SessionManager(session_manager_config)
        manager.on_no_motion(100.0)

        assert manager.get_active_session_count() == 0

    def test_tick_without_sessions(self, session_manager_config):
        """Tick without sessions doesn't error."""
        manager = SessionManager(session_manager_config)
        manager.tick(100.0)  # Should not raise

    def test_intermittent_motion_single_session(self, session_manager_config):
        """Intermittent motion during cooldown keeps extending same session."""
        manager = SessionManager(session_manager_config)

        # Motion starts
        manager.on_motion_detected(100.0)
        assert manager.get_active_session_count() == 1

        # Motion stops, enters cooldown
        manager.on_no_motion(105.0)
        assert len(manager.get_cooldown_sessions()) == 1

        # Motion resumes during cooldown - extends session
        manager.on_motion_detected(106.0)
        assert manager.get_active_session_count() == 1
        assert len(manager.get_recording_sessions()) == 1

        # Motion stops again
        manager.on_no_motion(108.0)
        assert len(manager.get_cooldown_sessions()) == 1

        # Still just one session through all this
        assert manager.get_active_session_count() == 1
