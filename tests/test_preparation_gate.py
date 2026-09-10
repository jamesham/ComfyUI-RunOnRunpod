"""The local plan must reject missing requirements before GPU endpoint work."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import output_transfer


def load_routes():
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("_preparation_tests_plugin")
    package.__path__ = [str(root)]
    s3 = ModuleType(f"{package.__name__}.s3_utils")
    for name in (
        "get_s3_client", "upload_file", "upload_file_dedup", "download_file",
        "delete_objects", "list_objects", "key_exists",
    ):
        setattr(s3, name, Mock(side_effect=AssertionError("unmocked storage call")))
    web = SimpleNamespace(Response=object, json_response=lambda data, **kwargs: data)
    route_table = SimpleNamespace(get=lambda path: lambda fn: fn, post=lambda path: lambda fn: fn)
    host = SimpleNamespace(instance=SimpleNamespace(routes=route_table))
    modules = {
        package.__name__: package,
        s3.__name__: s3,
        f"{package.__name__}.output_transfer": output_transfer,
        f"{package.__name__}.model_lookup": SimpleNamespace(lookup_model=Mock()),
        f"{package.__name__}.latency": SimpleNamespace(check_all_regions=Mock()),
        "aiohttp": SimpleNamespace(web=web, ClientSession=Mock()),
        "server": SimpleNamespace(PromptServer=host),
    }
    spec = importlib.util.spec_from_file_location(f"{package.__name__}.routes", root / "routes.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


class PreparationGateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes = load_routes()
        self.events = patch.object(self.routes, "_send_event").start()
        self.addCleanup(patch.stopall)
        self.settings = {
            "apiKey": "test-key", "endpointId": "gpu-endpoint", "bucketName": "volume",
            "s3AccessKey": "access", "s3SecretKey": "secret", "endpointUrl": "https://s3.example.invalid",
            "uploadMissingModels": True, "downloadModelsFromTheSource": False,
        }
        self.workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }

    async def test_unresolved_model_blocks_before_gpu_actions(self):
        with patch.object(self.routes, "key_exists", return_value=False), \
             patch.object(self.routes, "_find_model_file", return_value=None), \
             patch.object(self.routes, "_fetch_and_check_worker_version", new=AsyncMock()) as version, \
             patch.object(self.routes, "_check_node_compatibility", new=AsyncMock()) as nodes, \
             patch.object(self.routes, "_submit_workflow_to_runpod", new=AsyncMock()) as submit, \
             patch.object(self.routes, "_validate_runpod_health", new=AsyncMock()), \
             patch.object(self.routes, "_validate_s3", new=AsyncMock(return_value=Mock())):
            result = await self.routes._do_submit({
                "settings": self.settings, "workflow": self.workflow, "prep_id": "prep-1",
            })
        self.assertIn("no local copy or usable source", result["error"])
        version.assert_not_awaited()
        nodes.assert_not_awaited()
        submit.assert_not_awaited()

    async def test_disabled_preparation_requires_models_to_be_present(self):
        settings = dict(self.settings, uploadMissingModels=False)
        with patch.object(self.routes, "key_exists", return_value=False), \
             patch.object(self.routes, "_find_model_file", return_value="/local/base.safetensors"):
            with self.assertRaisesRegex(self.routes._SubmitError, "not present"):
                await self.routes._plan_model_preparation(
                    settings, "volume", Mock(), self.workflow, {}, "prep-1",
                )

    async def test_present_model_needs_no_gpu_fetch_or_upload(self):
        with patch.object(self.routes, "key_exists", return_value=True):
            preparation = await self.routes._plan_model_preparation(
                dict(self.settings, uploadMissingModels=False), "volume", Mock(), self.workflow, {}, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [])
        self.assertEqual(preparation.upload_queue, [])

    async def test_local_fallback_becomes_an_explicit_upload_action(self):
        with patch.object(self.routes, "key_exists", return_value=False), \
             patch.object(self.routes, "_find_model_file", return_value="/local/base.safetensors"):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, {}, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [])
        self.assertEqual(preparation.upload_queue, [
            ("checkpoints", "base.safetensors", "/local/base.safetensors"),
        ])

    async def test_workflow_metadata_produces_an_explicit_worker_fetch_action(self):
        metadata = {"base.safetensors": {"url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors"}}
        with patch.object(self.routes, "key_exists", return_value=False), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [{
            "source": "workflow",
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "dest_path": "models/checkpoints/base.safetensors",
            "expected_sha256": None,
            "auth": "hf",
        }])

    async def test_unsafe_reference_blocks_before_storage_or_gpu_work(self):
        unsafe_workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "../escape.safetensors"}},
        }
        with patch.object(self.routes, "key_exists") as exists:
            with self.assertRaisesRegex(self.routes._SubmitError, "unsafe or ambiguous"):
                await self.routes._plan_model_preparation(
                    self.settings, "volume", Mock(), unsafe_workflow, {}, "prep-1",
                )
        exists.assert_not_called()


if __name__ == "__main__":
    unittest.main()
