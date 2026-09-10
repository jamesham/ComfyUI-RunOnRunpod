"""Hermetic transfer tests: fake object streams and temporary local files only."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from output_transfer import OutputRetrievalError, download_file, local_output_path


class OutputTransferTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.dest = Path(self.directory) / "image.png"
        self.body = Mock()
        self.body.iter_chunks.return_value = iter([b"abc", b"def"])
        self.client = Mock()
        self.client.get_object.return_value = {"Body": self.body, "ContentLength": 6}
        for target in ("socket.create_connection", "socket.socket.connect"):
            network_patch = patch(target, side_effect=AssertionError("network forbidden"))
            network_patch.start()
            self.addCleanup(network_patch.stop)

    def download(self):
        download_file(self.client, "volume", "outputs/job/image.png", str(self.dest))

    def assert_no_partials(self):
        self.assertEqual(list(Path(self.directory).glob("*.part")), [])

    def test_installs_complete_file_and_closes_stream(self):
        self.download()
        self.assertEqual(self.dest.read_bytes(), b"abcdef")
        self.body.close.assert_called_once_with()
        self.client.get_object.assert_called_once_with(Bucket="volume", Key="outputs/job/image.png")
        self.assert_no_partials()

    def test_final_path_is_not_published_until_stream_is_complete(self):
        def chunks(_size):
            self.assertFalse(self.dest.exists())
            yield b"abc"
            self.assertFalse(self.dest.exists())
            yield b"def"

        self.body.iter_chunks.side_effect = chunks
        self.download()
        self.assertEqual(self.dest.read_bytes(), b"abcdef")

    def test_interrupted_download_preserves_existing_file(self):
        self.dest.write_bytes(b"previous complete output")

        def chunks(_size):
            yield b"abc"
            raise OSError("connection interrupted")

        self.body.iter_chunks.side_effect = chunks
        with self.assertRaises(OSError):
            self.download()
        self.assertEqual(self.dest.read_bytes(), b"previous complete output")
        self.body.close.assert_called_once_with()
        self.assert_no_partials()

    def test_short_and_oversized_streams_never_install(self):
        for size in (5, 7):
            with self.subTest(size=size):
                self.client.get_object.return_value["ContentLength"] = size
                self.body.iter_chunks.return_value = iter([b"abcdef"])
                with self.assertRaises(OutputRetrievalError):
                    self.download()
                self.assertFalse(self.dest.exists())
                self.assert_no_partials()

    def test_missing_or_invalid_length_is_not_safe_to_install(self):
        for length in (None, -1, "6", True):
            with self.subTest(length=length):
                self.client.get_object.return_value["ContentLength"] = length
                with self.assertRaises(OutputRetrievalError):
                    self.download()
                self.assertFalse(self.dest.exists())
                self.assert_no_partials()
        self.assertEqual(self.body.close.call_count, 4)

    def test_empty_file_is_valid_when_declared_empty(self):
        self.client.get_object.return_value["ContentLength"] = 0
        self.body.iter_chunks.return_value = iter([])
        self.download()
        self.assertEqual(self.dest.read_bytes(), b"")

    def test_flush_or_replace_failure_keeps_existing_output(self):
        for operation in ("fsync", "replace"):
            with self.subTest(operation=operation):
                self.dest.write_bytes(b"old")
                self.body.iter_chunks.return_value = iter([b"abcdef"])
                with patch.object(os, operation, side_effect=OSError("disk failure")):
                    with self.assertRaises(OSError):
                        self.download()
                self.assertEqual(self.dest.read_bytes(), b"old")
                self.assert_no_partials()

    def test_parent_creation_failure_does_not_request_remote_object(self):
        self.dest = Path(self.directory) / "parent" / "image.png"
        with patch("output_transfer.os.makedirs", side_effect=PermissionError):
            with self.assertRaises(PermissionError):
                self.download()
        self.client.get_object.assert_not_called()

    def test_local_output_paths_are_portable_and_confined(self):
        expected = os.path.join(os.path.abspath(self.directory), "job", "image.png")
        self.assertEqual(local_output_path(self.directory, "job/image.png"), expected)
        for path in ("", "/absolute.png", "../escape.png", "job/../escape.png",
                     "job//image.png", "job/./image.png", r"job\\image.png",
                     "C:/image.png", "job/image\0.png"):
            with self.subTest(path=path):
                with self.assertRaises(OutputRetrievalError):
                    local_output_path(self.directory, path)

    def test_local_output_path_cannot_follow_a_subdirectory_symlink(self):
        with tempfile.TemporaryDirectory() as outside:
            try:
                os.symlink(outside, Path(self.directory) / "linked")
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")
            with self.assertRaises(OutputRetrievalError):
                local_output_path(self.directory, "linked/image.png")


if __name__ == "__main__":
    unittest.main()
