"""Exercise real completion routes without ComfyUI, aiohttp, or cloud access."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, call, patch

import output_transfer


def load_routes():
    """Replace only host/dependency boundaries; execute the actual route module."""
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("_output_tests_plugin")
    package.__path__ = [str(root)]
    s3 = ModuleType(f"{package.__name__}.s3_utils")
    for name in (
        "get_s3_client", "upload_file", "upload_file_dedup", "delete_objects",
        "list_objects", "key_exists",
    ):
        setattr(s3, name, Mock(side_effect=AssertionError("unmocked storage call")))
    s3.download_file = output_transfer.download_file
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


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        return False


class OutputCompletionTests(unittest.IsolatedAsyncioTestCase):
    def start_patch(self, *args, **kwargs):
        patcher = patch(*args, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def start_object_patch(self, target, attribute, **kwargs):
        patcher = patch.object(target, attribute, **kwargs)
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def setUp(self):
        self.routes = load_routes()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.start_patch("builtins.print")
        self.start_patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
        self.start_patch("socket.socket.connect", side_effect=AssertionError("network forbidden"))
        self.client = Mock()
        self.make_client = self.start_object_patch(
            self.routes, "_make_s3_client", return_value=self.client,
        )
        self.start_object_patch(
            self.routes, "_get_output_directory", return_value=self.directory,
        )
        self.events = self.start_object_patch(self.routes, "_send_event")
        self.delete = self.start_object_patch(self.routes, "delete_objects")
        self.settings = {
            "apiKey": "test-only", "endpointId": "test-endpoint", "bucketName": "test-volume",
            "deleteInputsAfterJob": True, "deleteOutputsAfterJob": True,
        }
        self.inputs = {"input.png": "inputs/hash.png"}

    def fake_download(self, failures=()):
        def response(**kwargs):
            key = kwargs["Key"]
            body = Mock()
            body.iter_chunks.return_value = iter([b"bad" if key in failures else b"complete"])
            return {"Body": body, "ContentLength": 8}

        self.client.get_object.side_effect = response

    def provider_result(self, output=None):
        if output is None:
            output = {"status": "success", "output_files": ["job/a.png", "job/b.png"], "output_count": 2}
        result = {"status": "COMPLETED", "output": output}
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=result))
        session = SimpleNamespace(get=Mock(return_value=AsyncContext(response)))
        self.routes.aiohttp.ClientSession.return_value = AsyncContext(session)
        return result

    async def test_successful_retrieval_installs_then_cleans_outputs_and_inputs(self):
        self.fake_download()

        def check_installed(client, bucket, keys):
            self.assertEqual((Path(self.directory) / "job/a.png").read_bytes(), b"complete")
            self.assertEqual((Path(self.directory) / "job/b.png").read_bytes(), b"complete")

        self.delete.side_effect = check_installed
        files = await self.routes._download_and_cleanup(
            self.settings, ["job/a.png", "job/b.png"], self.inputs,
        )
        self.assertEqual(files, ["job/a.png", "job/b.png"])
        self.assertEqual(self.delete.call_args_list, [
            call(self.client, "test-volume", ["outputs/job/a.png", "outputs/job/b.png"]),
            call(self.client, "test-volume", ["inputs/hash.png"]),
        ])

    async def test_partial_failure_retains_entire_remote_set_for_retry(self):
        self.fake_download(failures={"outputs/job/b.png"})
        with self.assertRaisesRegex(output_transfer.OutputRetrievalError, "1 of 2"):
            await self.routes._download_and_cleanup(self.settings, ["job/a.png", "job/b.png"], self.inputs)
        self.delete.assert_not_called()
        self.assertEqual((Path(self.directory) / "job/a.png").read_bytes(), b"complete")
        self.assertFalse((Path(self.directory) / "job/b.png").exists())
        self.fake_download()
        self.assertEqual(await self.routes._download_and_cleanup(
            self.settings, ["job/a.png", "job/b.png"], self.inputs,
        ), ["job/a.png", "job/b.png"])

    async def test_client_failure_is_reported_without_cleanup(self):
        self.make_client.side_effect = RuntimeError("client failed")
        with self.assertRaisesRegex(output_transfer.OutputRetrievalError, "remote files were kept"):
            await self.routes._download_and_cleanup(self.settings, ["job/a.png"], self.inputs)
        self.delete.assert_not_called()

    async def test_cleanup_preferences_remain_independent(self):
        self.fake_download()
        for inputs, outputs in ((False, False), (True, False), (False, True)):
            with self.subTest(inputs=inputs, outputs=outputs):
                self.delete.reset_mock()
                settings = dict(self.settings, deleteInputsAfterJob=inputs, deleteOutputsAfterJob=outputs)
                await self.routes._download_and_cleanup(settings, ["job/a.png"], self.inputs)
                self.assertEqual(self.delete.call_count, int(inputs) + int(outputs))

    async def test_remote_cleanup_failure_does_not_hide_installed_output(self):
        self.fake_download()
        self.delete.side_effect = OSError("delete failed")
        self.assertEqual(await self.routes._download_and_cleanup(
            self.settings, ["job/a.png"], self.inputs,
        ), ["job/a.png"])

    async def test_cpu_retrieval_uses_artifacts_without_s3(self):
        operation = SimpleNamespace(
            endpoint_id="cpu-endpoint", volume_binding="volume-1",
        )
        settings = {
            "apiKey": "test-only", "endpointId": "gpu-endpoint", "stagingMode": "cpu",
            "managedSessionId": "session-1", "deleteInputsAfterJob": True,
            "deleteOutputsAfterJob": True,
        }
        with patch.object(self.routes, "_managed_cpu_artifact_operation", return_value=operation), \
             patch.object(self.routes, "download_cpu_artifact", new=AsyncMock()) as download, \
             patch.object(self.routes, "delete_cpu_artifact", new=AsyncMock()) as delete:
            files = await self.routes._download_and_cleanup_cpu(
                settings, ["job/a.png"], {"input.png": "inputs/input"},
            )
        self.assertEqual(files, ["job/a.png"])
        self.make_client.assert_not_called()
        self.assertEqual(download.await_args.args[:5], (
            "cpu-endpoint", "test-only", "volume-1",
            download.await_args.args[3], "outputs/job/a.png",
        ))
        self.assertEqual(delete.await_count, 2)

    async def poll(self):
        self.routes._active_tasks["job"] = object()
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            await self.routes._poll_and_finish("job", self.settings, self.inputs)
        self.assertNotIn("job", self.routes._active_tasks)

    async def test_live_completion_reports_retrieval_failure_not_success(self):
        self.provider_result()
        self.fake_download(failures={"outputs/job/b.png"})
        await self.poll()
        self.assertEqual(self.events.call_args.args[0], "failed")
        self.assertIn("remote files were kept", self.events.call_args.args[1]["error"])
        self.assertFalse(any(event.args[0] == "completed" for event in self.events.call_args_list))
        self.delete.assert_not_called()

    async def test_live_success_keeps_existing_frontend_event(self):
        self.provider_result()
        self.fake_download()
        await self.poll()
        self.events.assert_any_call("completed", {"job_id": "job", "files": ["job/a.png", "job/b.png"]})

    async def test_worker_application_failure_is_not_empty_success(self):
        self.provider_result({"error": "ComfyUI rejected workflow"})
        await self.poll()
        self.events.assert_called_once_with("failed", {"job_id": "job", "error": "ComfyUI rejected workflow"})
        self.client.get_object.assert_not_called()
        self.delete.assert_not_called()

    async def test_recovery_reports_failure_and_can_retry_same_remote_result(self):
        self.provider_result()
        request = SimpleNamespace(json=AsyncMock(return_value={"settings": self.settings, "job_ids": ["job"]}))
        self.fake_download(failures={"outputs/job/a.png"})
        result = await self.routes.recover_jobs(request)
        self.assertEqual(result["recovered"][0]["state"], "failed")
        self.delete.assert_not_called()
        self.fake_download()
        result = await self.routes.recover_jobs(request)
        self.assertEqual(result["recovered"][0], {
            "job_id": "job", "state": "completed", "files": ["job/a.png", "job/b.png"],
        })
        # Recovery never invents the lost input cleanup context.
        self.delete.assert_called_once_with(self.client, "test-volume", ["outputs/job/a.png", "outputs/job/b.png"])

    async def test_recovery_preserves_worker_error(self):
        self.provider_result({"error": "Execution failed"})
        request = SimpleNamespace(json=AsyncMock(return_value={"settings": self.settings, "job_ids": ["job"]}))
        result = await self.routes.recover_jobs(request)
        self.assertEqual(result["recovered"][0], {"job_id": "job", "state": "failed", "error": "Execution failed"})
        self.client.get_object.assert_not_called()

    def test_invalid_worker_outputs_are_rejected_but_zero_outputs_are_valid(self):
        for output in (
            None, [], {}, {"status": "success"},
            {"status": "success", "output_files": [None]},
            {"status": "success", "output_files": ["../escape.png"]},
        ):
            with self.subTest(output=output):
                with self.assertRaises(output_transfer.OutputRetrievalError):
                    self.routes._completed_output_files({"output": output})
        self.assertEqual(self.routes._completed_output_files({
            "output": {"status": "success", "output_files": [], "output_count": 0},
        }), [])


if __name__ == "__main__":
    unittest.main()
