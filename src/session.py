"""Session state machine for event recording."""

import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional


class SessionState(Enum):
    """States for a recording session."""

    RECORDING = "recording"      # Actively capturing (motion detected)
    COOLDOWN = "cooldown"        # No motion, waiting cooldown period
    FINALIZING = "finalizing"    # Concatenating clips to MP4
    COMPLETED = "completed"      # Session finished


@dataclass
class Session:
    """Represents a single recording session."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    state: SessionState = SessionState.RECORDING
    start_time: float = 0.0
    last_activity_time: float = 0.0
    cooldown_start_time: Optional[float] = None
    clips: List[Path] = field(default_factory=list)

    def enter_cooldown(self, current_time: float):
        """Transition to cooldown state."""
        if self.state == SessionState.RECORDING:
            self.state = SessionState.COOLDOWN
            self.cooldown_start_time = current_time

    def extend_recording(self, current_time: float):
        """Extend recording due to continued motion."""
        if self.state == SessionState.COOLDOWN:
            self.state = SessionState.RECORDING
            self.cooldown_start_time = None
        self.last_activity_time = current_time

    def should_finalize(self, current_time: float, cooldown_seconds: float) -> bool:
        """Check if cooldown has expired and session should finalize."""
        if self.state != SessionState.COOLDOWN:
            return False
        if self.cooldown_start_time is None:
            return False
        return (current_time - self.cooldown_start_time) >= cooldown_seconds

    def enter_finalizing(self):
        """Transition to finalizing state."""
        if self.state == SessionState.COOLDOWN:
            self.state = SessionState.FINALIZING

    def complete(self):
        """Mark session as completed."""
        self.state = SessionState.COMPLETED

    def add_clip(self, clip_path: Path):
        """Add a clip to this session."""
        self.clips.append(clip_path)

    @property
    def is_active(self) -> bool:
        """Check if session is still active (not completed)."""
        return self.state in (SessionState.RECORDING, SessionState.COOLDOWN, SessionState.FINALIZING)

    @property
    def is_recording(self) -> bool:
        """Check if session is actively recording."""
        return self.state == SessionState.RECORDING

    @property
    def is_in_cooldown(self) -> bool:
        """Check if session is in cooldown."""
        return self.state == SessionState.COOLDOWN
