"""Tests for the portable CPU-staging HMAC key generator."""

import importlib.util
from pathlib import Path
import re
import unittest


def _module():
    path = Path(__file__).resolve().parents[1] / "tools" / "generate_hmac_key.py"
    spec = importlib.util.spec_from_file_location("generate_hmac_key", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GenerateHmacKeyTests(unittest.TestCase):
    def test_key_is_url_safe_and_has_cryptographic_entropy_length(self):
        key = _module().generate_key()
        self.assertGreaterEqual(len(key), 64)
        self.assertIsNotNone(re.fullmatch(r"[A-Za-z0-9_-]+", key))

    def test_each_key_is_fresh(self):
        generator = _module().generate_key
        self.assertNotEqual(generator(), generator())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
