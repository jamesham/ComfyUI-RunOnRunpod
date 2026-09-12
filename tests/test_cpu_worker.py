"""Unit coverage for CPU worker outbound download headers."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch


def _load_handler() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "worker-cpu" / "handler.py"
    spec = importlib.util.spec_from_file_location("_cpu_worker_handler_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "runpod": ModuleType("runpod"),
        "requests": ModuleType("requests"),
        spec.name: module,
    }):
        spec.loader.exec_module(module)
    return module


class CpuWorkerHeaderTests(unittest.TestCase):
    def test_downloads_use_the_plugin_user_agent_with_provider_auth(self):
        handler = _load_handler()
        with patch.dict(handler.os.environ, {
            "HF_TOKEN": "hf-token", "CIVITAI_API_KEY": "civitai-token",
        }, clear=True):
            self.assertEqual(handler._headers("none"), {
                "User-Agent": "ComfyUI-RunOnRunpod",
            })
            self.assertEqual(handler._headers("hf"), {
                "User-Agent": "ComfyUI-RunOnRunpod", "Authorization": "Bearer hf-token",
            })
            self.assertEqual(handler._headers("civitai"), {
                "User-Agent": "ComfyUI-RunOnRunpod", "Authorization": "Bearer civitai-token",
            })
