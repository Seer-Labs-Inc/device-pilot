"""Platform abstraction for file watching and system operations."""

import logging
import subprocess
import sys
import tempfile
import threading
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Safe directory prefixes for cleanup operations
SAFE_CLEANUP_PREFIXES = [
    Path(tempfile.gettempdir()),  # /tmp or /var/folders/...
    Path("/mnt/ramdisk"),          # Raspberry Pi RAM disk
]


def is_safe_to_delete(path: Path) -> bool:
    """
    Check if a path is safe to delete.

    Only allows deletion of directories under:
    - System temp directory (/tmp, /var/folders/...)
    - RAM disk (/mnt/ramdisk)
    - Must contain 'device-pilot' in the path

    This prevents accidental deletion of project files or user data.
    """
    if not path.exists():
        return False

    # Resolve to absolute path
    resolved = path.resolve()

    # Must contain 'device-pilot' somewhere in the path
    if "device-pilot" not in str(resolved):
        logger.warning(f"Refusing to delete {resolved}: not a device-pilot directory")
        return False

    # Must be under a safe prefix
    for safe_prefix in SAFE_CLEANUP_PREFIXES:
        try:
            resolved.relative_to(safe_prefix.resolve())
            return True
        except ValueError:
            continue

    logger.warning(f"Refusing to delete {resolved}: not under a safe temp directory")
    return False


def safe_rmtree(path: Path) -> bool:
    """
    Safely remove a directory tree only if it passes safety checks.

    Returns True if deleted, False if skipped or failed.
    """
    if not is_safe_to_delete(path):
        return False

    try:
        shutil.rmtree(path, ignore_errors=True)
        logger.debug(f"Cleaned up directory: {path}")
        return True
    except Exception as e:
        logger.error(f"Failed to clean up {path}: {e}")
        return False


class WatcherHandle:
    """Handle to a running file watcher."""

    def __init__(self, process: subprocess.Popen, thread: Optional[threading.Thread] = None):
        self.process = process
        self.thread = thread
        self._stopped = False

    def stop(self):
        """Stop the file watcher."""
        if self._stopped:
            return
        self._stopped = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5)


class Platform(ABC):
    """Abstract base class for platform-specific operations."""

    @abstractmethod
    def start_file_watcher(
        self, directory: Path, callback: Callable[[Path], None], pattern: str = "*.ts"
    ) -> WatcherHandle:
        """Start watching a directory for new files."""
        pass

    @abstractmethod
    def setup_buffer_directory(self, path: Path) -> Path:
        """Set up the buffer directory."""
        pass

    @abstractmethod
    def cleanup_buffer_directory(self, path: Path):
        """Clean up the buffer directory."""
        pass

    @staticmethod
    def get_current() -> "Platform":
        """Get the platform implementation for the current system."""
        if sys.platform == "darwin":
            return MacPlatform()
        else:
            return LinuxPlatform()


class MacPlatform(Platform):
    """Mac-specific platform implementation using fswatch."""

    def start_file_watcher(
        self, directory: Path, callback: Callable[[Path], None], pattern: str = "*.ts"
    ) -> WatcherHandle:
        """Start fswatch to watch for new .ts files."""
        cmd = [
            "fswatch",
            "-0",  # NUL-separated output
            "--event", "Created",
            "--include", pattern.replace("*", ".*"),
            "--exclude", ".*",
            str(directory),
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def reader():
            while True:
                try:
                    if process.poll() is not None:
                        break
                    # Read NUL-separated paths
                    data = b""
                    while True:
                        char = process.stdout.read(1)
                        if not char or char == b"\0":
                            break
                        data += char
                    if data:
                        path = Path(data.decode().strip())
                        if path.exists():
                            callback(path)
                except Exception:
                    break

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        return WatcherHandle(process, thread)

    def setup_buffer_directory(self, path: Path) -> Path:
        """Set up buffer directory (just create it on Mac)."""
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cleanup_buffer_directory(self, path: Path):
        """Clean up buffer directory (only if safe)."""
        safe_rmtree(path)


class LinuxPlatform(Platform):
    """Linux/Raspberry Pi platform implementation using inotifywait."""

    RAMDISK_PATH = Path("/mnt/ramdisk")
    RAMDISK_SIZE = "200M"

    def _ensure_ramdisk(self) -> bool:
        """
        Ensure RAM disk is mounted at /mnt/ramdisk.

        Creates and mounts if it doesn't exist.
        Returns True if RAM disk is available, False otherwise.
        """
        # Check if already mounted
        try:
            result = subprocess.run(
                ["mountpoint", "-q", str(self.RAMDISK_PATH)],
                capture_output=True
            )
            if result.returncode == 0:
                logger.debug("RAM disk already mounted")
                return True
        except FileNotFoundError:
            pass  # mountpoint command not available

        # Try to create and mount
        try:
            # Create mount point if needed
            if not self.RAMDISK_PATH.exists():
                logger.info(f"Creating RAM disk mount point at {self.RAMDISK_PATH}")
                subprocess.run(
                    ["sudo", "mkdir", "-p", str(self.RAMDISK_PATH)],
                    check=True,
                    capture_output=True
                )

            # Mount tmpfs
            logger.info(f"Mounting {self.RAMDISK_SIZE} RAM disk at {self.RAMDISK_PATH}")
            subprocess.run(
                ["sudo", "mount", "-t", "tmpfs", "-o", f"size={self.RAMDISK_SIZE}",
                 "tmpfs", str(self.RAMDISK_PATH)],
                check=True,
                capture_output=True
            )

            # Make it writable by current user
            subprocess.run(
                ["sudo", "chmod", "777", str(self.RAMDISK_PATH)],
                check=True,
                capture_output=True
            )

            logger.info("RAM disk mounted successfully")
            return True

        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to mount RAM disk: {e}. Using temp directory instead.")
            return False
        except Exception as e:
            logger.warning(f"RAM disk setup failed: {e}. Using temp directory instead.")
            return False

    def start_file_watcher(
        self, directory: Path, callback: Callable[[Path], None], pattern: str = "*.ts"
    ) -> WatcherHandle:
        """Start inotifywait to watch for new .ts files."""
        cmd = [
            "inotifywait",
            "-m",  # Monitor continuously
            "-q",  # Quiet
            "-e", "close_write",
            "--format", "%w%f",
            str(directory),
        ]

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        def reader():
            while True:
                try:
                    if process.poll() is not None:
                        break
                    line = process.stdout.readline()
                    if not line:
                        break
                    path = Path(line.decode().strip())
                    # Check if it matches the pattern
                    if path.suffix == ".ts" and path.exists():
                        callback(path)
                except Exception:
                    break

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()

        return WatcherHandle(process, thread)

    def setup_buffer_directory(self, path: Path) -> Path:
        """
        Set up buffer directory, using RAM disk if possible.

        Automatically creates and mounts a RAM disk at /mnt/ramdisk if not present.
        Falls back to the provided path if RAM disk setup fails.
        """
        # Try to ensure RAM disk is available
        if self._ensure_ramdisk():
            # Use RAM disk path instead
            ramdisk_buffer = self.RAMDISK_PATH / "device-pilot" / "buffer"
            ramdisk_buffer.mkdir(parents=True, exist_ok=True)
            logger.info(f"Using RAM disk buffer: {ramdisk_buffer}")
            return ramdisk_buffer

        # Fall back to provided path
        path.mkdir(parents=True, exist_ok=True)
        logger.info(f"Using filesystem buffer: {path}")
        return path

    def cleanup_buffer_directory(self, path: Path):
        """Clean up buffer directory (only if safe)."""
        safe_rmtree(path)
