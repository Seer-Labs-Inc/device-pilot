"""Multi-session orchestration for overlapping events."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .session import Session, SessionState


@dataclass
class SessionManagerConfig:
    """Configuration for the session manager."""

    pre_roll_seconds: float = 10.0
    cooldown_seconds: float = 10.0


class SessionManager:
    """
    Manages multiple recording sessions, handling overlapping events.

    Key behavior for overlapping events:
    - If motion detected while a session is in RECORDING state: extend that session
    - If motion detected while all sessions are in COOLDOWN: start a NEW session
    - Each session has its own cooldown timer
    """

    def __init__(
        self,
        config: SessionManagerConfig,
        on_session_start: Optional[Callable[[Session], None]] = None,
        on_session_finalize: Optional[Callable[[Session], None]] = None,
    ):
        """
        Initialize the session manager.

        Args:
            config: Session timing configuration
            on_session_start: Callback when a new session starts
            on_session_finalize: Callback when a session needs finalization
        """
        self.config = config
        self.on_session_start = on_session_start
        self.on_session_finalize = on_session_finalize
        self.active_sessions: Dict[str, Session] = {}
        self.completed_sessions: List[Session] = []

    def on_motion_detected(self, current_time: Optional[float] = None):
        """
        Handle motion detection event.

        If any session is active (recording OR cooldown), extend it.
        Only start a new session if there are no active sessions.
        """
        if current_time is None:
            current_time = time.time()

        if self.active_sessions:
            # Extend all active sessions (brings cooldown sessions back to recording)
            for session in self.active_sessions.values():
                session.extend_recording(current_time)
        else:
            # No active sessions → start new session
            self._start_new_session(current_time)

    def on_no_motion(self, current_time: Optional[float] = None):
        """
        Handle no-motion event.

        All actively recording sessions enter cooldown.
        """
        if current_time is None:
            current_time = time.time()

        for session in self.active_sessions.values():
            if session.state == SessionState.RECORDING:
                session.enter_cooldown(current_time)

    def tick(self, current_time: Optional[float] = None):
        """
        Process session timers and finalize expired sessions.

        Should be called periodically (e.g., every second).
        """
        if current_time is None:
            current_time = time.time()

        sessions_to_finalize = []

        for session in list(self.active_sessions.values()):
            if session.should_finalize(current_time, self.config.cooldown_seconds):
                sessions_to_finalize.append(session)

        for session in sessions_to_finalize:
            self._finalize_session(session)

    def _start_new_session(self, current_time: float) -> Session:
        """Start a new recording session."""
        session = Session(
            start_time=current_time,
            last_activity_time=current_time,
        )
        self.active_sessions[session.id] = session

        if self.on_session_start:
            self.on_session_start(session)

        return session

    def _finalize_session(self, session: Session):
        """Finalize a session (trigger MP4 creation)."""
        session.enter_finalizing()

        if self.on_session_finalize:
            self.on_session_finalize(session)

        # Move to completed
        del self.active_sessions[session.id]
        session.complete()
        self.completed_sessions.append(session)

    def get_active_session_count(self) -> int:
        """Get the number of active sessions."""
        return len(self.active_sessions)

    def get_recording_sessions(self) -> List[Session]:
        """Get sessions that are actively recording."""
        return [s for s in self.active_sessions.values() if s.is_recording]

    def get_cooldown_sessions(self) -> List[Session]:
        """Get sessions that are in cooldown."""
        return [s for s in self.active_sessions.values() if s.is_in_cooldown]

    def add_clip_to_active_sessions(self, clip_path: Path):
        """Add a clip to all active sessions."""
        for session in self.active_sessions.values():
            session.add_clip(clip_path)
