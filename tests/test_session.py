"""Tests for session state machine."""

import pytest

from src.session import Session, SessionState


class TestSession:
    """Tests for Session class."""

    def test_session_initial_state(self):
        """New session starts in RECORDING state."""
        session = Session(start_time=100.0)
        assert session.state == SessionState.RECORDING
        assert session.is_active
        assert session.is_recording
        assert not session.is_in_cooldown

    def test_session_id_generated(self):
        """Session ID is auto-generated."""
        session = Session()
        assert session.id is not None
        assert len(session.id) == 8

    def test_enter_cooldown(self):
        """Session transitions to cooldown."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)

        assert session.state == SessionState.COOLDOWN
        assert session.cooldown_start_time == 110.0
        assert session.is_in_cooldown
        assert not session.is_recording

    def test_extend_recording_from_cooldown(self):
        """Session can return to recording from cooldown."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)
        session.extend_recording(115.0)

        assert session.state == SessionState.RECORDING
        assert session.cooldown_start_time is None
        assert session.last_activity_time == 115.0

    def test_should_finalize_after_cooldown(self):
        """Session should finalize when cooldown expires."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)

        # Not yet expired (cooldown = 5s)
        assert not session.should_finalize(112.0, cooldown_seconds=5.0)

        # Expired
        assert session.should_finalize(116.0, cooldown_seconds=5.0)

    def test_should_not_finalize_while_recording(self):
        """Recording session should not finalize."""
        session = Session(start_time=100.0)
        assert not session.should_finalize(200.0, cooldown_seconds=5.0)

    def test_enter_finalizing(self):
        """Session transitions to finalizing from cooldown."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)
        session.enter_finalizing()

        assert session.state == SessionState.FINALIZING
        assert session.is_active

    def test_complete(self):
        """Session completes and becomes inactive."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)
        session.enter_finalizing()
        session.complete()

        assert session.state == SessionState.COMPLETED
        assert not session.is_active

    def test_add_clip(self):
        """Clips can be added to session."""
        from pathlib import Path

        session = Session()
        clip1 = Path("/tmp/clip_0001.ts")
        clip2 = Path("/tmp/clip_0002.ts")

        session.add_clip(clip1)
        session.add_clip(clip2)

        assert len(session.clips) == 2
        assert clip1 in session.clips
        assert clip2 in session.clips


class TestSessionStateTransitions:
    """Tests for state transition edge cases."""

    def test_enter_cooldown_only_from_recording(self):
        """Cooldown only works from RECORDING state."""
        session = Session(start_time=100.0)
        session.enter_cooldown(110.0)
        session.enter_finalizing()

        # Try to enter cooldown again (should have no effect)
        old_state = session.state
        session.enter_cooldown(120.0)
        assert session.state == old_state

    def test_extend_recording_updates_activity_time(self):
        """Extending recording updates last activity time."""
        session = Session(start_time=100.0, last_activity_time=100.0)
        session.extend_recording(150.0)
        assert session.last_activity_time == 150.0

    def test_multiple_cooldown_entries(self):
        """Session can enter and exit cooldown multiple times."""
        session = Session(start_time=100.0)

        # First cooldown
        session.enter_cooldown(110.0)
        assert session.is_in_cooldown

        # Motion resumes
        session.extend_recording(112.0)
        assert session.is_recording

        # Second cooldown
        session.enter_cooldown(120.0)
        assert session.is_in_cooldown
        assert session.cooldown_start_time == 120.0
