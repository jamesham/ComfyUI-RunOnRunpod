"""Small standard-library file-locking and durability helpers.

The ComfyUI plugin runs on Windows as well as POSIX hosts.  Keep the platform
specific APIs contained here so ordinary plugin imports never require a POSIX
only module such as :mod:`fcntl`.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from typing import BinaryIO, Iterator

try:  # POSIX: Linux (including the CPU worker) and macOS.
    import fcntl as _fcntl
except ImportError:  # Windows does not ship fcntl.
    _fcntl = None

try:  # Windows CRT byte-range locking.
    import msvcrt as _msvcrt
except ImportError:  # POSIX hosts do not ship msvcrt.
    _msvcrt = None


class FileLockError(RuntimeError):
    """Raised only when the running Python platform has no supported lock API."""


_thread_locks_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def _thread_lock(handle: BinaryIO) -> threading.Lock:
    """Return a process-local lock for the lock-file name.

    Windows byte-range locks do not provide the thread coordination guarantees
    needed by this project on their own.  The local lock also makes behavior
    consistent with POSIX when two ComfyUI threads open the same lock file.
    """
    name = getattr(handle, "name", None)
    key = os.path.abspath(os.fspath(name)) if isinstance(name, (str, bytes, os.PathLike)) else str(handle.fileno())
    with _thread_locks_guard:
        return _thread_locks.setdefault(key, threading.Lock())


def _prepare_windows_lock(handle: BinaryIO) -> None:
    """Ensure that byte zero exists before locking it with ``msvcrt``."""
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)


def _lock_windows(handle: BinaryIO) -> None:
    if _msvcrt is None:
        raise FileLockError("this platform has no supported advisory file-lock API")
    _prepare_windows_lock(handle)
    while True:
        try:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
            return
        except OSError:
            # ``LK_NBLCK`` avoids the CRT's fixed one-second retry cadence.
            # The outer thread lock prevents same-process contention; this
            # loop is solely for another process holding the lock byte.
            time.sleep(0.05)


def _unlock_windows(handle: BinaryIO) -> None:
    if _msvcrt is None:
        raise FileLockError("this platform has no supported advisory file-lock API")
    handle.seek(0)
    _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def advisory_file_lock(handle: BinaryIO) -> Iterator[None]:
    """Exclusively lock an already-open binary lock file on every supported OS.

    Lock files should be opened with ``a+b``.  On Windows this helper reserves
    byte zero; that byte is lock metadata only and never appears in coordinator
    JSON records because lock files are separate from record files.
    """
    thread_lock = _thread_lock(handle)
    with thread_lock:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            unlock = lambda: _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
        else:
            _lock_windows(handle)
            unlock = lambda: _unlock_windows(handle)
        try:
            yield
        finally:
            unlock()


def fsync_directory(path: Path) -> None:
    """Persist a POSIX directory entry after an atomic replacement.

    Windows does not support opening and syncing directories through Python's
    ``os.open``/``os.fsync`` interface.  The replacement file itself is still
    flushed before publication; skipping this POSIX-only extra durability step
    is the portable behavior.
    """
    if _fcntl is None:
        return
    descriptor = os.open(os.fspath(path), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
