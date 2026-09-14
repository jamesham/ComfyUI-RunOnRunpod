"""The local plan must reject missing requirements before GPU endpoint work."""

import importlib.util
import hashlib
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import output_transfer
from cpu_staging_contract import stage_request_from_downloads


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
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.local_model = Path(self.tempdir.name) / "base.safetensors"
        self.local_model.write_bytes(b"known local model bytes")
        self.settings = {
            "apiKey": "test-key", "endpointId": "gpu-endpoint", "bucketName": "volume",
            "s3AccessKey": "access", "s3SecretKey": "secret", "endpointUrl": "https://s3.example.invalid",
            "uploadMissingModels": True, "downloadModelsFromTheSource": False,
        }
        self.workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }

    async def test_unresolved_model_blocks_before_gpu_actions(self):
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
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
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=str(self.local_model)):
            with self.assertRaisesRegex(self.routes._SubmitError, "not present"):
                await self.routes._plan_model_preparation(
                    settings, "volume", Mock(), self.workflow, {}, "prep-1",
                )

    async def test_present_model_needs_no_gpu_fetch_or_upload(self):
        with patch.object(self.routes, "load_matching_receipt", return_value=object()):
            preparation = await self.routes._plan_model_preparation(
                dict(self.settings, uploadMissingModels=False), "volume", Mock(), self.workflow, {}, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [])
        self.assertEqual(preparation.upload_queue, [])

    async def test_local_fallback_becomes_an_explicit_upload_action(self):
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=str(self.local_model)):
            preparation = await self.routes._plan_model_preparation(
            self.settings, "volume", Mock(), self.workflow, {}, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [])
        self.assertEqual(preparation.upload_queue, [
            ("checkpoints", "base.safetensors", str(self.local_model)),
        ])

    async def test_workflow_metadata_produces_an_explicit_worker_fetch_action(self):
        expected_sha256 = hashlib.sha256(b"remote bytes").hexdigest()
        metadata = {"base.safetensors": {
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "sha256": expected_sha256, "size": 12,
        }}
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        self.assertEqual(preparation.worker_downloads, [{
            "source": "workflow",
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "dest_path": "models/checkpoints/base.safetensors",
            "expected_sha256": expected_sha256,
            "expected_size": 12,
            "auth": "hf",
        }])

    async def test_cpu_stager_requires_a_request_for_exact_plan(self):
        expected_sha256 = hashlib.sha256(b"remote bytes").hexdigest()
        metadata = {"base.safetensors": {
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "sha256": expected_sha256, "size": 12,
        }}
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        payload = stage_request_from_downloads(
            "prep-1", "volume-1", preparation.worker_downloads,
        )
        configured = dict(
            self.settings, stagingMode="cpu", cpuStagerEndpointId="cpu-endpoint",
            cpuStagerVolumeBinding="volume-1",
        )
        configured["cpuStagerRequest"] = payload
        operation = self.routes._cpu_stager_request(configured, preparation, "prep-1")
        self.assertEqual(operation.endpoint_id, "cpu-endpoint")
        self.assertEqual(operation.stage_request, configured["cpuStagerRequest"])
        self.assertIsNone(operation.coordinator)
        configured["cpuStagerRequest"]["models"][0]["expected_size"] = 13
        with self.assertRaisesRegex(self.routes._SubmitError, "does not match"):
            self.routes._cpu_stager_request(configured, preparation, "prep-1")

    async def test_managed_session_gets_endpoint_and_request_from_server_coordinator(self):
        expected_sha256 = hashlib.sha256(b"remote bytes").hexdigest()
        metadata = {"base.safetensors": {
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "sha256": expected_sha256, "size": 12,
        }}
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        fake = Mock()
        fake.get_session.return_value = {
            "profile_id": "profile-1", "recipe_id": "recipe-1",
            "bindings": {"cpu_endpoint_id": "managed-cpu", "volume_binding": "volume-1"},
        }
        fake.prepare_stage_request.return_value = {"operation_id": "prep-1"}
        with patch.object(
            self.routes, "managed_configuration_from_settings",
            return_value=(fake, Mock(profile_id="profile-1"), "recipe-1"),
        ):
            operation = self.routes._cpu_stager_request(
                dict(self.settings, stagingMode="cpu", managedSessionId="session-1"), preparation, "prep-1",
            )
        self.assertEqual(operation.endpoint_id, "managed-cpu")
        self.assertEqual(operation.session_id, "session-1")
        self.assertIs(operation.coordinator, fake)
        fake.prepare_stage_request.assert_called_once_with(
            "session-1", "prep-1", preparation.worker_downloads,
        )

    async def test_gpu_mode_is_default_and_uses_legacy_worker_fetch(self):
        expected_sha256 = hashlib.sha256(b"remote bytes").hexdigest()
        metadata = {"base.safetensors": {
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "sha256": expected_sha256, "size": 12,
        }}
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        legacy_fetch = AsyncMock(return_value={"models/checkpoints/base.safetensors"})
        cpu_stage = AsyncMock()
        with patch.object(self.routes, "clear_receipt"), \
             patch.object(self.routes, "write_receipt"), \
             patch.object(self.routes, "load_matching_receipt", return_value=object()), \
             patch.object(self.routes, "_run_worker_fetches", new=legacy_fetch), \
             patch.object(self.routes, "stage_models_on_cpu", new=cpu_stage):
            await self.routes._execute_model_preparation(
                preparation,
                dict(self.settings, cpuStagerEndpointId="stale-cpu-endpoint"),
                "volume", Mock(), "gpu-endpoint", "test-key", "prep-1",
            )
        legacy_fetch.assert_awaited_once()
        cpu_stage.assert_not_awaited()

    async def test_cpu_mode_requires_a_cpu_staging_request(self):
        expected_sha256 = hashlib.sha256(b"remote bytes").hexdigest()
        metadata = {"base.safetensors": {
            "url": "https://huggingface.co/org/repo/resolve/commit/base.safetensors",
            "sha256": expected_sha256, "size": 12,
        }}
        with patch.object(self.routes, "load_matching_receipt", return_value=None), \
             patch.object(self.routes, "_find_model_file", return_value=None):
            preparation = await self.routes._plan_model_preparation(
                self.settings, "volume", Mock(), self.workflow, metadata, "prep-1",
            )
        with self.assertRaisesRegex(self.routes._SubmitError, "CPU managed staging requires"):
            self.routes._cpu_stager_request(
                dict(self.settings, stagingMode="cpu"), preparation, "prep-1",
            )

    async def test_cpu_submission_prepares_before_any_gpu_or_s3_call(self):
        settings = dict(
            self.settings, stagingMode="cpu", managedSessionId="session-1",
            managedProfile="{\"profile_version\": 1}",
        )
        for key in ("endpointId", "bucketName", "s3AccessKey", "s3SecretKey", "endpointUrl"):
            settings.pop(key)
        operation = SimpleNamespace(
            endpoint_id="cpu-endpoint", volume_binding="volume-1",
        )
        order = []

        async def plan(*_args):
            order.append("plan")
            return object()

        async def inputs(*_args):
            order.append("inputs")
            return {"image.png": "inputs/image"}

        async def execute(*_args):
            order.append("stage")

        async def health(*_args):
            order.append("health")

        async def binding(*_args):
            order.append("binding")

        async def provision(*_args):
            order.append("provision")
            return "managed-gpu"

        async def version(*_args):
            order.append("version")

        async def nodes(*_args):
            order.append("nodes")

        async def submit(*args):
            order.append("submit")
            self.assertEqual(args[0], "managed-gpu")
            self.assertEqual(args[4]["endpointId"], "managed-gpu")
            return {"job_id": "job-1"}

        with patch.object(self.routes, "_managed_cpu_artifact_operation", return_value=operation), \
             patch.object(self.routes, "_plan_cpu_model_preparation", new=plan), \
             patch.object(self.routes, "_upload_cpu_input_files", new=inputs), \
             patch.object(self.routes, "_execute_cpu_model_preparation", new=execute), \
             patch.object(self.routes, "_ensure_managed_gpu_endpoint", new=provision), \
             patch.object(self.routes, "_validate_cpu_gpu_volume", new=binding), \
             patch.object(self.routes, "_validate_runpod_health", new=health), \
             patch.object(self.routes, "_fetch_and_check_worker_version", new=version), \
             patch.object(self.routes, "_check_node_compatibility", new=nodes), \
             patch.object(self.routes, "_submit_workflow_to_runpod", new=submit), \
             patch.object(self.routes, "_validate_s3", new=AsyncMock(side_effect=AssertionError("CPU used S3"))):
            result = await self.routes._do_submit({
                "settings": settings, "workflow": {}, "prep_id": "prep-1",
            })
        self.assertEqual(result, {"job_id": "job-1"})
        self.assertEqual(order, [
            "plan", "inputs", "stage", "provision", "binding", "health", "version", "nodes", "submit",
        ])

    def test_invalid_staging_mode_is_rejected(self):
        with self.assertRaisesRegex(self.routes._SubmitError, "Staging mode"):
            self.routes._staging_mode(dict(self.settings, stagingMode="other"))

    async def test_unsafe_reference_blocks_before_storage_or_gpu_work(self):
        unsafe_workflow = {
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "../escape.safetensors"}},
        }
        with patch.object(self.routes, "load_matching_receipt") as ready:
            with self.assertRaisesRegex(self.routes._SubmitError, "unsafe or ambiguous"):
                await self.routes._plan_model_preparation(
                    self.settings, "volume", Mock(), unsafe_workflow, {}, "prep-1",
                )
        ready.assert_not_called()


if __name__ == "__main__":
    unittest.main()
