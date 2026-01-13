"""Tests for recording management."""

import shutil
from pathlib import Path

import pytest

from src.buffer import ClipInfo, HLSBuffer, copy_clips
from src.recorder import RecorderManager, SessionRecorder
from tests.conftest import requires_ffmpeg, requires_ffprobe


class TestClipInfo:
    """Tests for ClipInfo dataclass."""

    def test_clip_info_creation(self, tmp_path):
        """ClipInfo stores clip metadata."""
        clip_path = tmp_path / "clip_0001.ts"
        clip_path.touch()

        info = ClipInfo(
            path=clip_path,
            index=1,
            timestamp=1234567890.0,
        )

        assert info.path == clip_path
        assert info.index == 1
        assert info.timestamp == 1234567890.0


class TestCopyClips:
    """Tests for copy_clips utility function."""

    def test_copy_clips_creates_destination(self, tmp_path):
        """copy_clips creates destination directory."""
        source = tmp_path / "source"
        source.mkdir()

        clip = source / "clip_0001.ts"
        clip.write_bytes(b"test data")

        dest = tmp_path / "dest" / "subdir"
        copy_clips([clip], dest)

        assert dest.exists()

    def test_copy_clips_copies_files(self, tmp_path):
        """copy_clips copies files to destination."""
        source = tmp_path / "source"
        source.mkdir()

        clips = []
        for i in range(3):
            clip = source / f"clip_{i:04d}.ts"
            clip.write_bytes(f"data {i}".encode())
            clips.append(clip)

        dest = tmp_path / "dest"
        copied = copy_clips(clips, dest)

        assert len(copied) == 3
        for i, copied_clip in enumerate(copied):
            assert copied_clip.exists()
            assert copied_clip.read_bytes() == f"data {i}".encode()


class TestSessionRecorder:
    """Tests for SessionRecorder class."""

    def test_session_recorder_creation(self, tmp_path):
        """SessionRecorder initializes correctly."""
        recorder = SessionRecorder(
            session_id="test123",
            session_dir=tmp_path / "session",
            evidence_dir=tmp_path / "evidence",
        )

        assert recorder.session_id == "test123"
        assert len(recorder.clips) == 0

    def test_add_clip_copies_to_session(self, tmp_path):
        """Adding a clip copies it to session directory."""
        source = tmp_path / "source"
        source.mkdir()
        clip = source / "clip_0001.ts"
        clip.write_bytes(b"video data")

        session_dir = tmp_path / "session"
        session_dir.mkdir()

        recorder = SessionRecorder(
            session_id="test123",
            session_dir=session_dir,
            evidence_dir=tmp_path / "evidence",
        )

        recorder.add_clip(clip)

        assert len(recorder.clips) == 1
        assert recorder.clips[0].exists()
        assert recorder.clips[0].parent == session_dir

    def test_add_clip_deduplication(self, tmp_path):
        """Adding same clip twice doesn't duplicate."""
        source = tmp_path / "source"
        source.mkdir()
        clip = source / "clip_0001.ts"
        clip.write_bytes(b"video data")

        session_dir = tmp_path / "session"
        session_dir.mkdir()

        recorder = SessionRecorder(
            session_id="test123",
            session_dir=session_dir,
            evidence_dir=tmp_path / "evidence",
        )

        recorder.add_clip(clip)
        recorder.add_clip(clip)

        assert len(recorder.clips) == 1

    @requires_ffmpeg
    def test_finalize_creates_mp4(self, hls_buffer, tmp_path):
        """Finalize creates MP4 from clips."""
        session_dir = tmp_path / "session"
        session_dir.mkdir()
        evidence_dir = tmp_path / "evidence"

        recorder = SessionRecorder(
            session_id="test123",
            session_dir=session_dir,
            evidence_dir=evidence_dir,
        )

        # Add clips from HLS buffer
        for clip in sorted(hls_buffer.glob("clip_*.ts"))[:3]:
            recorder.add_clip(clip)

        output_path = recorder.finalize()

        assert output_path is not None
        assert output_path.exists()
        assert output_path.suffix == ".mp4"

    def test_finalize_no_clips_returns_none(self, tmp_path):
        """Finalize with no clips returns None."""
        recorder = SessionRecorder(
            session_id="test123",
            session_dir=tmp_path / "session",
            evidence_dir=tmp_path / "evidence",
        )

        result = recorder.finalize()
        assert result is None

    def test_cleanup_removes_session_dir(self, tmp_path):
        """Cleanup removes session directory (only if path contains 'device-pilot')."""
        # Session dir must contain 'device-pilot' to pass safety check
        session_dir = tmp_path / "device-pilot" / "sessions" / "test123"
        session_dir.mkdir(parents=True)
        (session_dir / "clip.ts").touch()

        recorder = SessionRecorder(
            session_id="test123",
            session_dir=session_dir,
            evidence_dir=tmp_path / "evidence",
        )

        recorder.cleanup()
        assert not session_dir.exists()


class TestRecorderManager:
    """Tests for RecorderManager class."""

    def test_manager_creation(self, tmp_path):
        """RecorderManager initializes correctly."""
        manager = RecorderManager(
            buffer_dir=tmp_path / "buffer",
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        assert len(manager.recorders) == 0

    def test_start_session_creates_recorder(self, tmp_path):
        """Starting a session creates a recorder."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        manager = RecorderManager(
            buffer_dir=buffer_dir,
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        recorder = manager.start_session("session1", [])
        assert recorder is not None
        assert "session1" in manager.recorders

    def test_start_session_with_preroll(self, tmp_path):
        """Starting a session copies pre-roll clips."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        # Create some pre-roll clips
        preroll_clips = []
        for i in range(3):
            clip = buffer_dir / f"clip_{i:04d}.ts"
            clip.write_bytes(b"preroll data")
            preroll_clips.append(clip)

        manager = RecorderManager(
            buffer_dir=buffer_dir,
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        recorder = manager.start_session("session1", preroll_clips)
        assert len(recorder.clips) == 3

    def test_add_clip_to_sessions(self, tmp_path):
        """Adding clip distributes to all active sessions."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        manager = RecorderManager(
            buffer_dir=buffer_dir,
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        # Start two sessions
        manager.start_session("session1", [])
        manager.start_session("session2", [])

        # Add a clip
        clip = buffer_dir / "new_clip.ts"
        clip.write_bytes(b"new clip data")
        manager.add_clip_to_sessions(clip)

        # Both sessions should have the clip
        assert len(manager.recorders["session1"].clips) == 1
        assert len(manager.recorders["session2"].clips) == 1

    @requires_ffmpeg
    def test_finalize_session(self, hls_buffer, tmp_path):
        """Finalizing a session creates MP4 and removes recorder."""
        manager = RecorderManager(
            buffer_dir=hls_buffer,
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        # Start session with pre-roll
        preroll = list(sorted(hls_buffer.glob("clip_*.ts"))[:3])
        manager.start_session("session1", preroll)

        # Finalize
        output = manager.finalize_session("session1")

        assert output is not None
        assert output.exists()
        assert "session1" not in manager.recorders

    def test_cleanup(self, tmp_path):
        """Cleanup removes all sessions."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        manager = RecorderManager(
            buffer_dir=buffer_dir,
            sessions_dir=tmp_path / "sessions",
            evidence_dir=tmp_path / "evidence",
        )

        manager.start_session("session1", [])
        manager.start_session("session2", [])

        manager.cleanup()

        assert len(manager.recorders) == 0


class TestHLSBuffer:
    """Tests for HLSBuffer class."""

    def test_buffer_initialization(self, tmp_path):
        """HLSBuffer initializes with configuration."""
        buffer = HLSBuffer(
            rtsp_url="rtsp://test/stream",
            buffer_dir=tmp_path / "buffer",
            segment_duration=5.0,
            max_segments=10,
        )

        assert buffer.rtsp_url == "rtsp://test/stream"
        assert buffer.segment_duration == 5.0
        assert buffer.max_segments == 10

    def test_get_clips_empty_buffer(self, tmp_path):
        """Getting clips from empty buffer returns empty list."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        buffer = HLSBuffer(
            rtsp_url="rtsp://test/stream",
            buffer_dir=buffer_dir,
        )

        clips = buffer.get_clips()
        assert clips == []

    def test_get_clips_sorted_by_index(self, tmp_path):
        """Clips are returned sorted by index."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        # Create clips out of order
        for i in [3, 1, 4, 2]:
            (buffer_dir / f"clip_{i:04d}.ts").touch()

        buffer = HLSBuffer(
            rtsp_url="rtsp://test/stream",
            buffer_dir=buffer_dir,
        )

        clips = buffer.get_clips()
        indices = [c.index for c in clips]
        assert indices == [1, 2, 3, 4]

    def test_get_preroll_clips(self, tmp_path):
        """Pre-roll returns correct number of clips."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        # Create 10 clips
        for i in range(10):
            (buffer_dir / f"clip_{i:04d}.ts").touch()

        buffer = HLSBuffer(
            rtsp_url="rtsp://test/stream",
            buffer_dir=buffer_dir,
            segment_duration=5.0,
        )

        # Request 15 seconds of pre-roll (3 clips @ 5s each)
        preroll = buffer.get_preroll_clips(15.0)
        assert len(preroll) == 4  # ceil(15/5) + 1 for safety

    def test_get_latest_clip(self, tmp_path):
        """Getting latest clip returns highest index."""
        buffer_dir = tmp_path / "buffer"
        buffer_dir.mkdir()

        for i in range(5):
            (buffer_dir / f"clip_{i:04d}.ts").touch()

        buffer = HLSBuffer(
            rtsp_url="rtsp://test/stream",
            buffer_dir=buffer_dir,
        )

        latest = buffer.get_latest_clip()
        assert latest.name == "clip_0004.ts"
