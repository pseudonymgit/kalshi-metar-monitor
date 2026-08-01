"""
Lock File Utility — prevents overlapping cron runs.

Usage:
    from core.lock_file import LockFile, with_lock

    # Context manager
    with LockFile('paper_trading') as lock:
        if lock.acquired:
            # do work
            pass
        else:
            logger.warning("Previous run still in progress, skipping")

    # Decorator
    @with_lock('daily_sweep')
    def run_sweep():
        pass

The lock file contains the PID of the owning process. Stale locks
(process no longer running) are automatically cleaned up.
"""

import os
import time
import logging
import atexit
from contextlib import contextmanager
from typing import Optional, Generator, Callable

logger = logging.getLogger(__name__)

LOCK_DIR = os.environ.get(
    'WEATHER_ENGINE_LOCK_DIR',
    '/tmp/weather_engine_locks'
)
LOCK_STALE_SECONDS = 3600  # 1 hour — locks older than this are cleaned up


class LockFile:
    """
    Process-level lock file for cron overlap protection.

    Usage:
        lock = LockFile('paper_trading')
        if lock.acquire():
            try:
                # do work
                pass
            finally:
                lock.release()
        else:
            logger.warning("Lock held by PID %s", lock.lock_pid())
    """

    def __init__(self, name: str, timeout: float = 0, stale_seconds: int = LOCK_STALE_SECONDS):
        """
        Args:
            name: Lock name (becomes /tmp/weather_engine_locks/{name}.lock)
            timeout: If > 0, block up to `timeout` seconds waiting for lock
            stale_seconds: Locks older than this are considered stale
        """
        self.name = name
        self.timeout = timeout
        self.stale_seconds = stale_seconds
        self._lock_path = os.path.join(LOCK_DIR, f"{name}.lock")
        self._acquired = False
        self._ensure_lock_dir()

    def _ensure_lock_dir(self) -> None:
        """Create lock directory if it doesn't exist."""
        os.makedirs(LOCK_DIR, exist_ok=True)

    def acquire(self) -> bool:
        """
        Try to acquire the lock. Returns True if acquired.

        Stale locks (old PID no longer running) are auto-cleaned.
        """
        if self._acquired:
            return True  # Already held

        deadline = time.time() + self.timeout if self.timeout > 0 else None

        while True:
            # Check if lock exists and is valid
            if os.path.exists(self._lock_path):
                # Read the lock file
                try:
                    with open(self._lock_path, 'r') as f:
                        content = f.read().strip()
                    parts = content.split(':')
                    lock_pid = int(parts[0]) if parts else 0
                    lock_time = float(parts[1]) if len(parts) > 1 else 0.0
                except (ValueError, OSError):
                    lock_pid = 0
                    lock_time = 0.0

                # Check if lock is stale
                is_stale = False
                if lock_pid > 0:
                    # Check if process is still running
                    if not self._pid_exists(lock_pid):
                        is_stale = True
                elif lock_time > 0 and (time.time() - lock_time) > self.stale_seconds:
                    is_stale = True

                if is_stale:
                    logger.info(
                        "Removing stale lock '%s' from PID %s (age: %.0fs)",
                        self.name, lock_pid or 'unknown',
                        time.time() - lock_time if lock_time > 0 else 0
                    )
                    os.unlink(self._lock_path)
                elif deadline is not None and time.time() < deadline:
                    # Wait and retry
                    time.sleep(min(1.0, deadline - time.time()))
                    continue
                else:
                    return False  # Lock held by another process

            # Try to create the lock file atomically
            try:
                with open(self._lock_path, 'w') as f:
                    f.write(f"{os.getpid()}:{time.time()}")
                self._acquired = True
                # Register cleanup on process exit
                atexit.register(self.release)
                logger.debug("Acquired lock '%s' (PID %s)", self.name, os.getpid())
                return True
            except OSError as e:
                logger.error("Failed to create lock '%s': %s", self.name, e)
                return False

    def release(self) -> None:
        """Release the lock."""
        if self._acquired:
            try:
                if os.path.exists(self._lock_path):
                    os.unlink(self._lock_path)
                self._acquired = False
                logger.debug("Released lock '%s'", self.name)
            except OSError as e:
                logger.error("Failed to release lock '%s': %s", self.name, e)

    @property
    def acquired(self) -> bool:
        return self._acquired

    def lock_pid(self) -> Optional[int]:
        """Return the PID holding the lock, or None if not locked."""
        if os.path.exists(self._lock_path):
            try:
                with open(self._lock_path, 'r') as f:
                    return int(f.read().strip().split(':')[0])
            except (ValueError, OSError):
                pass
        return None

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        """Check if a PID is running (Unix)."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def __enter__(self) -> 'LockFile':
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


@contextmanager
def with_lock(name: str, timeout: float = 0) -> Generator[bool, None, None]:
    """
    Context manager for lock-based cron overlap protection.

    Usage:
        with with_lock('paper_trading') as acquired:
            if not acquired:
                return  # Skip this run
            # do work
    """
    lock = LockFile(name, timeout=timeout)
    acquired = lock.acquire()
    try:
        yield acquired
    finally:
        lock.release()


def lock_decorator(name: str, timeout: float = 0) -> Callable:
    """
    Decorator for locking a function.

    Usage:
        @lock_decorator('daily_sweep')
        def run_sweep():
            pass
    """
    def decorator(func: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            with with_lock(name, timeout=timeout) as acquired:
                if not acquired:
                    logger.warning("Lock '%s' not acquired, skipping %s", name, func.__name__)
                    return None
                return func(*args, **kwargs)
        return wrapper
    return decorator