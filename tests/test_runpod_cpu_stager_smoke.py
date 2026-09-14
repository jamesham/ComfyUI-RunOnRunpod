"""Unit coverage for live CPU-stager harness configuration and cleanup."""

import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from integration.runpod_cpu_stager_smoke import (
    _SUCCESS_PREPARATION_ID,
    _debug_api_call,
    _profile,
    _secret_from_file_or_prompt,
    _secret_reference,
    run,
)


class RunPodCpuStagerSmokeTests(unittest.TestCase):
    def test_profile_uses_only_provider_secret_references(self):
        arguments = SimpleNamespace(
            data_center="dc-1", volume_size_gb=10, cpu_image="image@sha256:abc",
            cpu_template_id="template-cpu", cpu_flavor_id=["cpu3c"], vcpu_count=4,
            idle_timeout_seconds=5, execution_timeout_seconds=1800,
            provider="hf", provider_secret_name="smoke_hf",
        )
        profile = _profile(arguments, "session-1")
        self.assertEqual(dict(profile.cpu_environment), {
            "HF_TOKEN": "{{ RUNPOD_SECRET_smoke_hf }}",
        })

    def test_secret_reference_rejects_unsafe_name(self):
        with self.assertRaisesRegex(ValueError, "secret names"):
            _secret_reference("bad secret")

    def test_secret_can_be_read_from_a_file_without_using_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret.txt"
            path.write_text("secret-value\n", encoding="utf-8")
            self.assertEqual(_secret_from_file_or_prompt(str(path), "unused"), "secret-value")

    def test_debug_output_redacts_credentials_but_shows_stage_request(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            _debug_api_call("POST", "/serverless", {
                "Authorization": "api-key", "env": {"HF_TOKEN": "provider-token"},
                "stage_request": {"models": [{"url": "https://example.test/model"}]},
            }, 201, {"token": "result-token", "id": "endpoint-1"})
        value = output.getvalue()
        self.assertIn('"status": 201', value)
        self.assertIn('"id": "endpoint-1"', value)
        self.assertNotIn("api-key", value)
        self.assertNotIn("provider-token", value)
        self.assertNotIn("result-token", value)
        self.assertIn("https://example.test/model", value)

    def test_run_stages_once_records_result_and_cleans_exact_session(self):
        coordinator = Mock()
        coordinator.prepare_stage_request.return_value = {"operation_id": _SUCCESS_PREPARATION_ID}
        service = Mock()
        service.start.return_value = {
            "session_id": "smoke-test", "state": "preparing",
            "bindings": {"volume_id": "volume-1", "cpu_endpoint_id": "cpu-1"},
        }
        service.end.return_value = {"session_id": "smoke-test", "state": "closed", "bindings": {}}
        stage_result = {"status": "success", "results": []}
        with tempfile.TemporaryDirectory() as directory, \
             patch("integration.runpod_cpu_stager_smoke.uuid.uuid4", return_value=SimpleNamespace(hex="test")), \
             patch("integration.runpod_cpu_stager_smoke.SessionCoordinator", return_value=coordinator), \
             patch("integration.runpod_cpu_stager_smoke.RunPodLifecycleAdapter"), \
             patch("integration.runpod_cpu_stager_smoke.SessionLifecycleService", return_value=service), \
             patch("integration.runpod_cpu_stager_smoke.stage_models", new_callable=AsyncMock, return_value=stage_result) as stage, \
             patch("integration.runpod_cpu_stager_smoke._secret_from_file_or_prompt", return_value="api"), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run(self._live_arguments(directory)), 0)
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(stage.call_args.args[:3], (
            "cpu-1", "api", {"operation_id": _SUCCESS_PREPARATION_ID},
        ))
        coordinator.prepare_stage_request.assert_called_once()
        coordinator.record_stage_result.assert_called_once_with(
            "smoke-test", _SUCCESS_PREPARATION_ID, stage_result,
        )
        service.end.assert_called_once_with("smoke-test")

    @staticmethod
    def _live_arguments(state_root: str) -> list[str]:
        return [
            "--live", "--data-center", "dc-1", "--cpu-template-id", "template-cpu",
            "--cpu-image", "image@sha256:abc", "--cpu-flavor-id", "cpu3c", "--vcpu-count", "4",
            "--provider-secret-name", "provider", "--download-url", "https://example.test/base",
            "--sha256", "a" * 64, "--size", "1", "--state-root", state_root,
        ]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
