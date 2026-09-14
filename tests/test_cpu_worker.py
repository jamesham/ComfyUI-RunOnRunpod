"""Unit coverage for CPU worker outbound download headers."""

import importlib.util
import base64
import hashlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

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

    def test_handler_installs_reads_and_deletes_artifacts_without_s3(self):
        handler = _load_handler()
        content = b"local content sent through the serverless API"
        digest = hashlib.sha256(content).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            handler.VOLUME_BINDING = "volume-1"
            handler.VOLUME_DIR = directory
            handler.runpod.serverless = SimpleNamespace(progress_update=Mock())
            first = content[:8]
            target = "inputs/asset-1"
            common = {
                "protocol_version": 1, "operation_id": "prep-1", "volume_binding": "volume-1",
                "action": "write", "target_path": target, "transfer_id": "transfer-1",
                "expected_sha256": digest, "expected_size": len(content),
            }
            partial = {
                **common, "offset": 0, "data": base64.b64encode(first).decode("ascii"), "complete": False,
            }
            complete = {
                **common, "offset": len(first), "data": base64.b64encode(content[len(first):]).decode("ascii"), "complete": True,
            }
            self.assertEqual(handler.handler({"input": {"artifact_request": partial}})["status"], "success")
            written = handler.handler({"input": {"artifact_request": complete}})
            self.assertEqual(written["status"], "success")
            self.assertEqual(written["sha256"], digest)
            self.assertEqual((Path(directory) / target).read_bytes(), content)

            read = {
                "protocol_version": 1, "operation_id": "read-1", "volume_binding": "volume-1",
                "action": "read", "target_path": target, "offset": 0, "length": 1024,
            }
            received = handler.handler({"input": {"artifact_request": read}})
            self.assertEqual(base64.b64decode(received["data"]), content)
            self.assertTrue(received["complete"])

            delete = {
                "protocol_version": 1, "operation_id": "delete-1", "volume_binding": "volume-1",
                "action": "delete", "target_path": target,
            }
            self.assertEqual(handler.handler({"input": {"artifact_request": delete}})["status"], "success")
            self.assertFalse((Path(directory) / target).exists())

    def test_stage_and_artifact_upload_report_cache_hits_for_matching_content(self):
        handler = _load_handler()
        content = b"cached"
        with tempfile.TemporaryDirectory() as directory:
            handler.VOLUME_BINDING = "volume-1"
            handler.VOLUME_DIR = directory
            handler.runpod.serverless = SimpleNamespace(progress_update=Mock())
            destination = Path(directory) / "models/checkpoints/model.bin"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            stage = {
                "protocol_version": 1, "operation_id": "prep-1", "volume_binding": "volume-1",
                "models": [{
                    "target_path": "models/checkpoints/model.bin", "url": "https://example.test/model",
                    "expected_sha256": digest, "expected_size": len(content), "auth": "none",
                }],
            }
            stage_result = handler.handler({"input": {"stage_request": stage}})
            self.assertTrue(stage_result["results"][0]["cache_hit"])
            artifact = {
                "protocol_version": 1, "operation_id": "upload-1", "volume_binding": "volume-1",
                "action": "write", "target_path": "models/checkpoints/model.bin",
                "transfer_id": "transfer-1", "offset": 0,
                "data": base64.b64encode(content).decode("ascii"), "complete": False,
                "expected_sha256": digest, "expected_size": len(content),
            }
            artifact_result = handler.handler({"input": {"artifact_request": artifact}})
            self.assertTrue(artifact_result["cache_hit"])
