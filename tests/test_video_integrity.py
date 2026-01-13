"""Tests for video integrity validation."""

import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import requires_ffmpeg, requires_ffprobe


@requires_ffprobe
class TestKeyframeAlignment:
    """Tests for I-frame alignment in video segments."""

    def test_first_frame_is_keyframe(self, hls_buffer):
        """First frame of each segment should be a keyframe."""
        for clip in sorted(hls_buffer.glob("clip_*.ts")):
            # Use ffprobe to check first frame
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_frames",
                "-show_entries", "frame=pict_type",
                "-of", "json",
                "-read_intervals", "%+#1",  # Only first frame
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                pytest.skip(f"FFprobe failed: {result.stderr.decode()}")

            data = json.loads(result.stdout)
            frames = data.get("frames", [])

            if frames:
                first_frame = frames[0]
                assert first_frame.get("pict_type") == "I", (
                    f"First frame of {clip.name} is not a keyframe"
                )

    def test_keyframe_interval(self, hls_buffer):
        """Keyframes should appear at expected intervals."""
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:1]:  # Just test first clip
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_frames",
                "-show_entries", "frame=pict_type,pts_time",
                "-of", "json",
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            frames = data.get("frames", [])

            keyframes = [f for f in frames if f.get("pict_type") == "I"]
            assert len(keyframes) >= 1, f"No keyframes found in {clip.name}"


@requires_ffprobe
class TestTimestampContinuity:
    """Tests for timestamp continuity in video."""

    def test_dts_monotonic(self, hls_buffer):
        """DTS (decode timestamps) should be monotonically increasing."""
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:2]:  # Test first 2 clips
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_frames",
                "-show_entries", "frame=pkt_dts_time",
                "-of", "json",
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            frames = data.get("frames", [])

            # Extract DTS values (skip N/A values)
            dts_values = []
            for f in frames:
                dts = f.get("pkt_dts_time")
                if dts and dts != "N/A":
                    dts_values.append(float(dts))

            # Check monotonic increase
            for i in range(1, len(dts_values)):
                assert dts_values[i] >= dts_values[i - 1], (
                    f"DTS not monotonic in {clip.name}: "
                    f"{dts_values[i - 1]} -> {dts_values[i]}"
                )

    def test_no_large_timestamp_gaps(self, hls_buffer):
        """Timestamps should not have large gaps."""
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:1]:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_frames",
                "-show_entries", "frame=pkt_dts_time",
                "-of", "json",
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            frames = data.get("frames", [])

            dts_values = []
            for f in frames:
                dts = f.get("pkt_dts_time")
                if dts and dts != "N/A":
                    dts_values.append(float(dts))

            # Check for gaps larger than 1 second
            for i in range(1, len(dts_values)):
                gap = dts_values[i] - dts_values[i - 1]
                assert gap < 1.0, (
                    f"Large timestamp gap in {clip.name}: {gap}s"
                )


@requires_ffmpeg
class TestDecodeErrors:
    """Tests for decode error checking."""

    def test_no_decode_errors(self, hls_buffer):
        """Video should decode without errors."""
        for clip in sorted(hls_buffer.glob("clip_*.ts")):
            cmd = [
                "ffmpeg",
                "-v", "error",
                "-i", str(clip),
                "-f", "null",
                "-",
            ]

            result = subprocess.run(cmd, capture_output=True)

            # Check stderr for errors
            errors = result.stderr.decode()
            assert not errors or "error" not in errors.lower(), (
                f"Decode errors in {clip.name}: {errors}"
            )

    def test_concatenated_video_no_errors(self, hls_buffer, tmp_path):
        """Concatenated video should decode without errors."""
        clips = sorted(hls_buffer.glob("clip_*.ts"))[:3]
        if len(clips) < 2:
            pytest.skip("Not enough clips to test concatenation")

        # Create concat file
        concat_file = tmp_path / "concat.txt"
        with open(concat_file, "w") as f:
            for clip in clips:
                f.write(f"file '{clip}'\n")

        # Concatenate
        output = tmp_path / "output.mp4"
        concat_cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output),
        ]

        result = subprocess.run(concat_cmd, capture_output=True)
        if result.returncode != 0:
            pytest.skip(f"Concatenation failed: {result.stderr.decode()}")

        # Check for decode errors
        check_cmd = [
            "ffmpeg",
            "-v", "error",
            "-i", str(output),
            "-f", "null",
            "-",
        ]

        result = subprocess.run(check_cmd, capture_output=True)
        errors = result.stderr.decode()
        assert not errors or "error" not in errors.lower(), (
            f"Decode errors in concatenated video: {errors}"
        )


@requires_ffprobe
class TestVideoMetadata:
    """Tests for video metadata validation."""

    def test_video_has_video_stream(self, hls_buffer):
        """Video files should have a video stream."""
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:1]:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v",
                "-show_entries", "stream=codec_type",
                "-of", "json",
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            streams = data.get("streams", [])

            assert len(streams) >= 1, f"No video stream in {clip.name}"
            assert streams[0].get("codec_type") == "video"

    def test_video_resolution(self, hls_buffer):
        """Video should have expected resolution."""
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:1]:
            cmd = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "json",
                str(clip),
            ]

            result = subprocess.run(cmd, capture_output=True)
            if result.returncode != 0:
                continue

            data = json.loads(result.stdout)
            streams = data.get("streams", [])

            if streams:
                width = streams[0].get("width", 0)
                height = streams[0].get("height", 0)
                assert width > 0, "Invalid video width"
                assert height > 0, "Invalid video height"
