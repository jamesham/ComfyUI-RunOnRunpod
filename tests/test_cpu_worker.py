"""Unit coverage for CPU worker outbound download headers."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import patch

from cpu_staging_contract import sign_stage_request

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

    def test_handler_refuses_a_corrupted_signature_before_staging(self):
        handler = _load_handler()
        handler.SIGNING_KEY = "test-hmac"
        handler.VOLUME_BINDING = "volume-1"
        envelope = sign_stage_request({
            "protocol_version": 1, "operation_id": "prep-1", "volume_binding": "volume-1",
            "models": [{
                "target_path": "models/checkpoints/base.safetensors", "url": "https://example.test/base",
                "expected_sha256": "a" * 64, "expected_size": 1, "auth": "none",
            }],
        }, "test-hmac")
        replacement = "0" if envelope["signature"][0] != "0" else "1"
        envelope["signature"] = replacement + envelope["signature"][1:]

        result = handler.handler({"input": {"signed_request": envelope}})

        self.assertEqual(result["status"], "failed")
        self.assertIn("signature is invalid", result["error"])
