"""Cross-platform tests for the coordinator/worker file-lock abstraction."""

from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordinator import SessionCoordinator
import file_lock


class _FakeMsvcrt:
    LK_NBLCK = 1
    LK_UNLCK = 2

    def __init__(self) -> None:
        self.calls: list[tuple[int, int, int]] = []

    def locking(self, descriptor: int, operation: int, size: int) -> None:
        self.calls.append((descriptor, operation, size))


class FileLockTests(unittest.TestCase):
    def test_module_imports_when_posix_fcntl_is_unavailable(self):
        path = Path(file_lock.__file__)
        spec = importlib.util.spec_from_file_location("_no_fcntl_file_lock", path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        original_import = builtins.__import__

        def without_posix_locks(name, *args, **kwargs):
            if name in {"fcntl", "msvcrt"}:
                raise ModuleNotFoundError(f"No module named '{name}'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=without_posix_locks):
            spec.loader.exec_module(module)
        self.assertIsNone(module._fcntl)
        self.assertIsNone(module._msvcrt)

    def test_windows_fallback_locks_and_unlocks_byte_zero(self):
        fake_msvcrt = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.lock"
            with patch.object(file_lock, "_fcntl", None), \
                 patch.object(file_lock, "_msvcrt", fake_msvcrt), \
                 open(path, "a+b") as handle:
                with file_lock.advisory_file_lock(handle):
                    self.assertEqual(path.read_bytes(), b"\0")
        self.assertEqual([call[1] for call in fake_msvcrt.calls], [
            fake_msvcrt.LK_NBLCK, fake_msvcrt.LK_UNLCK,
        ])

    def test_windows_fallback_skips_posix_directory_sync(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(file_lock, "_fcntl", None), \
                 patch.object(file_lock.os, "open", side_effect=AssertionError("POSIX directory open")):
                file_lock.fsync_directory(Path(directory))

    def test_windows_fallback_supports_coordinator_state_writes(self):
        fake_msvcrt = _FakeMsvcrt()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(file_lock, "_fcntl", None), \
             patch.object(file_lock, "_msvcrt", fake_msvcrt):
            coordinator = SessionCoordinator(directory)
            saved = coordinator.save_profile("profile-1", {"safe": "value"})
            self.assertEqual(saved["revision"], 1)
            self.assertEqual(coordinator.get_profile("profile-1")["safe"], "value")
        self.assertIn(fake_msvcrt.LK_NBLCK, [call[1] for call in fake_msvcrt.calls])

    def test_native_lock_releases_after_context_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.lock"
            with open(path, "a+b") as handle:
                with file_lock.advisory_file_lock(handle):
                    pass
                with file_lock.advisory_file_lock(handle):
                    pass


if __name__ == "__main__":
    unittest.main()
