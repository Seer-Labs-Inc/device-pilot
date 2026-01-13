"""Motion and light detection using OpenCV."""

from collections import deque
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass
class DetectionResult:
    """Result of analyzing a frame for motion/light events."""

    motion_detected: bool
    light_event_detected: bool
    motion_score: float  # Raw score for current frame
    smoothed_motion_score: float  # Smoothed score used for decision
    brightness: float
    brightness_delta: float


class Detector:
    """Detects motion and light changes in video frames."""

    # Number of frames to use for motion smoothing (at 30 FPS, 15 frames = 0.5 seconds)
    SMOOTHING_WINDOW = 15

    # Hysteresis: once motion is detected, require this many consecutive
    # low-motion frames before declaring "no motion" (at 30 FPS, 30 frames = 1 second)
    HYSTERESIS_FRAMES = 30

    def __init__(
        self,
        motion_threshold: float = 0.02,
        light_jump_threshold: float = 30.0,
    ):
        """
        Initialize the detector.

        Args:
            motion_threshold: Fraction of pixels that must change to detect motion (0-1)
            light_jump_threshold: Brightness change required to detect light event (0-255)
        """
        self.motion_threshold = motion_threshold
        self.light_jump_threshold = light_jump_threshold

        # Background subtractor for motion detection
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=16,
            detectShadows=False,
        )

        # Track brightness for light detection
        self._last_brightness: Optional[float] = None

        # Motion smoothing
        self._motion_scores: deque = deque(maxlen=self.SMOOTHING_WINDOW)
        self._motion_state: bool = False  # Current motion state (with hysteresis)
        self._low_motion_count: int = 0  # Consecutive frames below threshold

    def analyze_frame(self, frame: np.ndarray) -> DetectionResult:
        """
        Analyze a frame for motion and light events.

        Uses smoothing and hysteresis to prevent flickering:
        - Motion score is averaged over SMOOTHING_WINDOW frames
        - Once motion is detected, requires HYSTERESIS_FRAMES consecutive
          low-motion frames before declaring "no motion"

        Args:
            frame: BGR image as numpy array

        Returns:
            DetectionResult with detection flags and scores
        """
        # Convert to grayscale for analysis
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Motion detection using background subtraction
        fg_mask = self.bg_subtractor.apply(gray)
        motion_pixels = np.count_nonzero(fg_mask)
        total_pixels = fg_mask.size
        raw_motion_score = motion_pixels / total_pixels

        # Add to smoothing window
        self._motion_scores.append(raw_motion_score)

        # Calculate smoothed score (average of recent frames)
        smoothed_score = sum(self._motion_scores) / len(self._motion_scores)

        # Apply hysteresis for motion state
        if smoothed_score > self.motion_threshold:
            # Motion detected - immediately switch to motion state
            self._motion_state = True
            self._low_motion_count = 0
        else:
            # Below threshold
            if self._motion_state:
                # Currently in motion state - count consecutive low frames
                self._low_motion_count += 1
                if self._low_motion_count >= self.HYSTERESIS_FRAMES:
                    # Enough consecutive low frames - declare no motion
                    self._motion_state = False
            # If not in motion state, stay in no-motion state

        # Light detection using brightness change
        brightness = float(np.mean(gray))
        brightness_delta = 0.0

        if self._last_brightness is not None:
            brightness_delta = abs(brightness - self._last_brightness)

        light_event_detected = brightness_delta > self.light_jump_threshold
        self._last_brightness = brightness

        return DetectionResult(
            motion_detected=self._motion_state,
            light_event_detected=light_event_detected,
            motion_score=raw_motion_score,
            smoothed_motion_score=smoothed_score,
            brightness=brightness,
            brightness_delta=brightness_delta,
        )

    def reset(self):
        """Reset the detector state."""
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500,
            varThreshold=16,
            detectShadows=False,
        )
        self._last_brightness = None
        self._motion_scores.clear()
        self._motion_state = False
        self._low_motion_count = 0


class RTSPCapture:
    """Captures frames from an RTSP stream."""

    def __init__(self, url: str):
        """
        Initialize the RTSP capture.

        Args:
            url: RTSP stream URL
        """
        self.url = url
        self._cap: Optional[cv2.VideoCapture] = None

    def open(self) -> bool:
        """Open the RTSP stream."""
        self._cap = cv2.VideoCapture(self.url)
        return self._cap.isOpened()

    def read(self) -> tuple[bool, Optional[np.ndarray]]:
        """Read a frame from the stream."""
        if self._cap is None:
            return False, None
        return self._cap.read()

    def release(self):
        """Release the capture."""
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
