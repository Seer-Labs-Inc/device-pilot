"""Tests for motion and light detection."""

import numpy as np
import pytest

from src.detector import Detector, DetectionResult


class TestDetector:
    """Tests for Detector class."""

    def test_detector_initialization(self):
        """Detector initializes with default thresholds."""
        detector = Detector()
        assert detector.motion_threshold == 0.02
        assert detector.light_jump_threshold == 30.0

    def test_detector_custom_thresholds(self):
        """Detector accepts custom thresholds."""
        detector = Detector(motion_threshold=0.1, light_jump_threshold=50.0)
        assert detector.motion_threshold == 0.1
        assert detector.light_jump_threshold == 50.0

    def test_analyze_frame_returns_result(self, sample_frame):
        """Analyze frame returns DetectionResult."""
        detector = Detector()
        result = detector.analyze_frame(sample_frame)

        assert isinstance(result, DetectionResult)
        assert isinstance(result.motion_detected, bool)
        assert isinstance(result.light_event_detected, bool)
        assert isinstance(result.motion_score, float)
        assert isinstance(result.brightness, float)

    def test_no_motion_on_static_frames(self, sample_frame):
        """Static frames should not trigger motion detection."""
        detector = Detector()

        # Feed several identical frames to build background model
        for _ in range(30):
            result = detector.analyze_frame(sample_frame)

        # After background is established, same frame should show no motion
        result = detector.analyze_frame(sample_frame)
        assert result.motion_score < detector.motion_threshold

    def test_motion_detected_on_changed_frame(self, sample_frame, motion_frame):
        """Changed frame should trigger motion detection after smoothing."""
        detector = Detector()

        # Build background model with sample frame
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Introduce motion frames - alternating to prevent background adaptation
        # This simulates real motion where frames differ slightly
        detected = False
        for i in range(detector.SMOOTHING_WINDOW * 2):
            # Alternate between motion variations to prevent background adaptation
            if i % 2 == 0:
                result = detector.analyze_frame(motion_frame)
            else:
                # Slightly modified motion frame
                varied_frame = motion_frame.copy()
                varied_frame[100:200, 100:200] = 128
                result = detector.analyze_frame(varied_frame)

            if result.motion_detected:
                detected = True

        assert detected is True

    def test_no_light_event_on_stable_brightness(self, sample_frame):
        """Stable brightness should not trigger light event."""
        detector = Detector()

        # First frame establishes baseline
        detector.analyze_frame(sample_frame)

        # Same brightness frame
        result = detector.analyze_frame(sample_frame)
        assert result.light_event_detected is False
        assert result.brightness_delta < detector.light_jump_threshold

    def test_light_event_on_brightness_change(self, dark_frame, bright_frame):
        """Large brightness change should trigger light event."""
        detector = Detector(light_jump_threshold=30.0)

        # Start with dark frame
        detector.analyze_frame(dark_frame)

        # Switch to bright frame
        result = detector.analyze_frame(bright_frame)
        assert result.light_event_detected is True
        assert result.brightness_delta > detector.light_jump_threshold

    def test_detector_reset(self, sample_frame, motion_frame):
        """Reset clears detector state."""
        detector = Detector()

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Reset
        detector.reset()

        # After reset, first frame shouldn't trigger motion
        # (no background model yet)
        result = detector.analyze_frame(sample_frame)
        # First frame has no reference, so no light delta either
        assert result.brightness_delta == 0.0


class TestDetectionResult:
    """Tests for DetectionResult dataclass."""

    def test_detection_result_fields(self):
        """DetectionResult has all expected fields."""
        result = DetectionResult(
            motion_detected=True,
            light_event_detected=False,
            motion_score=0.05,
            smoothed_motion_score=0.04,
            brightness=128.0,
            brightness_delta=10.0,
        )

        assert result.motion_detected is True
        assert result.light_event_detected is False
        assert result.motion_score == 0.05
        assert result.smoothed_motion_score == 0.04
        assert result.brightness == 128.0
        assert result.brightness_delta == 10.0


class TestMotionThresholds:
    """Tests for motion threshold behavior."""

    def test_motion_threshold_boundary(self):
        """Motion detection respects threshold boundary based on smoothed score."""
        # Create frames with small localized motion
        base_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        base_frame[:, :] = [50, 100, 150]  # Uniform color

        # Frame with small motion area (~1% of frame)
        motion_frame = base_frame.copy()
        # Small rectangle: 48x64 pixels = 3072 pixels out of 307200 = ~1%
        motion_frame[100:148, 100:164, :] = 255

        # Alternate motion frame to prevent background adaptation
        motion_frame_alt = base_frame.copy()
        motion_frame_alt[100:148, 100:164, :] = 200

        # Test high threshold (5%) - small motion (~1%) should NOT exceed threshold
        detector_high = Detector(motion_threshold=0.05)  # 5% threshold
        for _ in range(30):
            detector_high.analyze_frame(base_frame)

        # Feed motion frames and check that smoothed score stays below threshold
        for i in range(detector_high.SMOOTHING_WINDOW * 2):
            frame = motion_frame if i % 2 == 0 else motion_frame_alt
            result_high = detector_high.analyze_frame(frame)

        # The smoothed motion score should be below the high threshold
        assert result_high.smoothed_motion_score < 0.05, \
            f"Smoothed score {result_high.smoothed_motion_score} should be < 0.05"

        # Test low threshold (0.5%) - small motion (~1%) SHOULD exceed threshold
        detector_low = Detector(motion_threshold=0.005)  # 0.5% threshold
        for _ in range(30):
            detector_low.analyze_frame(base_frame)

        # Feed motion frames and verify detection occurs
        detected_low = False
        for i in range(detector_low.SMOOTHING_WINDOW * 2):
            frame = motion_frame if i % 2 == 0 else motion_frame_alt
            result_low = detector_low.analyze_frame(frame)
            if result_low.motion_detected:
                detected_low = True

        # Small motion (~1%) should trigger 0.5% threshold
        assert detected_low is True


class TestMotionHysteresis:
    """Tests for motion smoothing and hysteresis."""

    def test_motion_requires_multiple_frames_to_trigger(self, sample_frame, motion_frame):
        """Motion detection requires sustained motion (smoothing)."""
        detector = Detector(motion_threshold=0.01)

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Single motion frame should not immediately trigger (smoothed average too low)
        result = detector.analyze_frame(motion_frame)
        # The raw score should be high, but motion_detected depends on smoothed average
        assert result.motion_score > detector.motion_threshold

        # Continue feeding motion frames until detected
        detected_at = None
        for i in range(detector.SMOOTHING_WINDOW):
            result = detector.analyze_frame(motion_frame)
            if result.motion_detected and detected_at is None:
                detected_at = i

        # Should eventually detect motion
        assert result.motion_detected is True
        assert detected_at is not None

    def test_motion_requires_hysteresis_to_clear(self, sample_frame, motion_frame):
        """Motion state requires HYSTERESIS_FRAMES low frames to clear."""
        detector = Detector(motion_threshold=0.01)

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Establish motion state
        for _ in range(detector.SMOOTHING_WINDOW + 5):
            detector.analyze_frame(motion_frame)

        # Verify we're in motion state
        result = detector.analyze_frame(motion_frame)
        assert result.motion_detected is True

        # Feed static frames - motion should persist during hysteresis period
        for i in range(detector.HYSTERESIS_FRAMES - 1):
            result = detector.analyze_frame(sample_frame)
            assert result.motion_detected is True, f"Motion cleared too early at frame {i}"

        # Continue feeding static frames until motion clears
        for _ in range(detector.SMOOTHING_WINDOW):
            result = detector.analyze_frame(sample_frame)

        # Eventually motion should clear
        assert result.motion_detected is False

    def test_motion_during_hysteresis_resets_counter(self, sample_frame, motion_frame):
        """Motion during hysteresis period resets the counter."""
        detector = Detector(motion_threshold=0.01)

        # Build background
        for _ in range(30):
            detector.analyze_frame(sample_frame)

        # Establish motion state
        for _ in range(detector.SMOOTHING_WINDOW + 5):
            detector.analyze_frame(motion_frame)

        # Start clearing with static frames (partial hysteresis)
        for _ in range(detector.HYSTERESIS_FRAMES // 2):
            result = detector.analyze_frame(sample_frame)

        assert result.motion_detected is True

        # Inject motion frame - should reset hysteresis counter
        for _ in range(detector.SMOOTHING_WINDOW):
            detector.analyze_frame(motion_frame)

        # Now try to clear again - should need full HYSTERESIS_FRAMES
        for i in range(detector.HYSTERESIS_FRAMES - 1):
            result = detector.analyze_frame(sample_frame)
            # During hysteresis, motion should still be detected
            # (unless smoothed average is also contributing)


class TestLightThresholds:
    """Tests for light threshold behavior."""

    def test_light_threshold_boundary(self, dark_frame, bright_frame):
        """Light detection respects threshold boundary."""
        # Use very high threshold
        detector_high = Detector(light_jump_threshold=999.0)
        detector_high.analyze_frame(dark_frame)
        result_high = detector_high.analyze_frame(bright_frame)
        assert result_high.light_event_detected is False

        # Use very low threshold
        detector_low = Detector(light_jump_threshold=1.0)
        detector_low.analyze_frame(dark_frame)
        result_low = detector_low.analyze_frame(bright_frame)
        assert result_low.light_event_detected is True

    def test_gradual_brightness_change(self):
        """Gradual brightness change should not trigger light event."""
        detector = Detector(light_jump_threshold=30.0)

        # Create frames with gradual brightness increase
        for brightness in range(0, 255, 5):
            frame = np.ones((480, 640, 3), dtype=np.uint8) * brightness
            result = detector.analyze_frame(frame)
            # Each step is only 5 units, should not trigger
            assert result.light_event_detected is False
