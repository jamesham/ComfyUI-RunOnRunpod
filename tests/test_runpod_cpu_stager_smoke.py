"""Unit coverage for the live CPU-stager harness configuration only."""

from types import SimpleNamespace
import contextlib
import io
import os
import unittest
from unittest.mock import Mock, patch

from cpu_stager_client import CpuStagerError
from integration.runpod_cpu_stager_smoke import _corrupt_signature, _debug_api_call, _profile, _secret_reference, run


class RunPodCpuStagerSmokeTests(unittest.TestCase):
    def test_profile_uses_secret_references_not_secret_values(self):
        arguments = SimpleNamespace(
            data_center="dc-1", volume_size_gb=10, cpu_image="image@sha256:abc",
            cpu_template_id="template-cpu", cpu_flavor_id=["cpu3c"], vcpu_count=4,
            idle_timeout_seconds=5, execution_timeout_seconds=1800,
            hmac_secret_name="smoke_hmac", provider="hf", provider_secret_name="smoke_hf",
        )
        profile = _profile(arguments, "session-1")
        self.assertEqual(dict(profile.cpu_environment), {
            "STAGING_REQUEST_HMAC_KEY": "{{ RUNPOD_SECRET_smoke_hmac }}",
            "HF_TOKEN": "{{ RUNPOD_SECRET_smoke_hf }}",
        })

    def test_secret_reference_rejects_unsafe_name(self):
        with self.assertRaisesRegex(ValueError, "secret names"):
            _secret_reference("bad secret")

    def test_debug_output_redacts_credentials_and_environment_values(self):
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            _debug_api_call("POST", "/serverless", {
                "Authorization": "api-key", "env": {"HF_TOKEN": "provider-token"},
                "signed_request": {
                    "payload": {"models": [{"url": "https://example.test/model"}]},
                    "signature": "signed-envelope",
                },
            }, 201, {"token": "result-token", "id": "endpoint-1"})
        value = output.getvalue()
        self.assertIn('"status": 201', value)
        self.assertIn('"id": "endpoint-1"', value)
        self.assertNotIn("api-key", value)
        self.assertNotIn("provider-token", value)
        self.assertNotIn("result-token", value)
        self.assertIn("signed-envelope", value)
        self.assertIn("https://example.test/model", value)

    def test_debug_flag_passes_the_trace_callback_to_both_runpod_clients(self):
        coordinator = Mock()
        service = Mock()
        service.start.return_value = {
            "session_id": "smoke-test", "state": "preparing",
            "bindings": {"volume_id": "volume-1", "cpu_endpoint_id": "cpu-1"},
        }
        service.end.return_value = {"session_id": "smoke-test", "state": "closed", "bindings": {}}
        coordinator.authorize_stage.return_value = {"payload": "signed"}
        arguments = self._live_arguments() + ["--debug"]
        with patch("integration.runpod_cpu_stager_smoke.uuid.uuid4", return_value=SimpleNamespace(hex="test")), \
             patch("integration.runpod_cpu_stager_smoke.SessionCoordinator", return_value=coordinator), \
             patch("integration.runpod_cpu_stager_smoke.RunPodLifecycleAdapter") as adapter, \
             patch("integration.runpod_cpu_stager_smoke.SessionLifecycleService", return_value=service), \
             patch("integration.runpod_cpu_stager_smoke.stage_models", new_callable=Mock, return_value={"status": "success", "results": []}) as stage, \
             patch("integration.runpod_cpu_stager_smoke.asyncio.run", return_value={"status": "success", "results": []}), \
             patch.dict(os.environ, {"RUNPOD_API_KEY": "api", "RUNONRUNPOD_CPU_STAGING_SIGNING_KEY": "hmac"}, clear=False):
            self.assertEqual(run(arguments), 0)
        self.assertIs(adapter.call_args.kwargs["debug"], stage.call_args.kwargs["on_api_call"])

    def test_corrupt_signature_preserves_the_payload_but_changes_the_signature(self):
        envelope = {"payload": {"operation_id": "smoke-stage"}, "signature": "a" * 64}
        corrupted = _corrupt_signature(envelope)
        self.assertEqual(corrupted["payload"], envelope["payload"])
        self.assertNotEqual(corrupted["signature"], envelope["signature"])
        self.assertEqual(corrupted["signature"][1:], envelope["signature"][1:])

    def test_corrupt_signature_flag_requires_the_worker_to_refuse_it(self):
        coordinator = Mock()
        service = Mock()
        service.start.return_value = {
            "session_id": "smoke-test", "state": "preparing",
            "bindings": {"volume_id": "volume-1", "cpu_endpoint_id": "cpu-1"},
        }
        service.end.return_value = {"session_id": "smoke-test", "state": "closed", "bindings": {}}
        coordinator.authorize_stage.return_value = {
            "payload": {"operation_id": "smoke-stage"}, "signature": "a" * 64,
        }
        output = io.StringIO()
        with patch("integration.runpod_cpu_stager_smoke.uuid.uuid4", return_value=SimpleNamespace(hex="test")), \
             patch("integration.runpod_cpu_stager_smoke.SessionCoordinator", return_value=coordinator), \
             patch("integration.runpod_cpu_stager_smoke.RunPodLifecycleAdapter"), \
             patch("integration.runpod_cpu_stager_smoke.SessionLifecycleService", return_value=service), \
             patch("integration.runpod_cpu_stager_smoke.stage_models", side_effect=CpuStagerError("signature is invalid")) as stage, \
             patch.dict(os.environ, {"RUNPOD_API_KEY": "api", "RUNONRUNPOD_CPU_STAGING_SIGNING_KEY": "hmac"}, clear=False), \
             contextlib.redirect_stdout(output):
            self.assertEqual(run(self._live_arguments() + ["--corrupt-signature"]), 0)
        self.assertNotEqual(stage.call_args.args[2]["signature"], "a" * 64)
        coordinator.record_stage_result.assert_not_called()
        service.end.assert_called_once_with("smoke-test")
        self.assertIn("SIGNATURE REJECTED", output.getvalue())

    def test_run_records_result_then_cleans_exact_session(self):
        coordinator = Mock()
        service = Mock()
        service.start.return_value = {
            "session_id": "smoke-test", "state": "preparing",
            "bindings": {"volume_id": "volume-1", "cpu_endpoint_id": "cpu-1"},
        }
        service.end.return_value = {"session_id": "smoke-test", "state": "closed", "bindings": {}}
        coordinator.authorize_stage.return_value = {"payload": "signed"}
        stage_result = {"status": "success", "results": []}
        arguments = self._live_arguments()
        output = io.StringIO()
        with patch("integration.runpod_cpu_stager_smoke.uuid.uuid4", return_value=SimpleNamespace(hex="test")), \
             patch("integration.runpod_cpu_stager_smoke.SessionCoordinator", return_value=coordinator), \
             patch("integration.runpod_cpu_stager_smoke.RunPodLifecycleAdapter"), \
             patch("integration.runpod_cpu_stager_smoke.SessionLifecycleService", return_value=service), \
             patch("integration.runpod_cpu_stager_smoke.stage_models", new_callable=Mock, return_value=stage_result) as stage, \
             patch("integration.runpod_cpu_stager_smoke.asyncio.run", return_value=stage_result), \
             patch.dict(os.environ, {"RUNPOD_API_KEY": "api", "RUNONRUNPOD_CPU_STAGING_SIGNING_KEY": "hmac"}, clear=False), \
             contextlib.redirect_stdout(output):
            self.assertEqual(run(arguments), 0)
        self.assertEqual(stage.call_args.args[:3], ("cpu-1", "api", {"payload": "signed"}))
        coordinator.record_stage_result.assert_called_once_with("smoke-test", "smoke-stage", stage_result)
        service.end.assert_called_once_with("smoke-test")
        self.assertIn("CLEANED", output.getvalue())

    def test_run_cleans_session_after_staging_failure(self):
        coordinator = Mock()
        service = Mock()
        service.start.return_value = {
            "session_id": "smoke-test", "state": "preparing",
            "bindings": {"volume_id": "volume-1", "cpu_endpoint_id": "cpu-1"},
        }
        service.end.return_value = {"session_id": "smoke-test", "state": "closed", "bindings": {}}
        coordinator.authorize_stage.return_value = {"payload": "signed"}
        errors = io.StringIO()
        with patch("integration.runpod_cpu_stager_smoke.uuid.uuid4", return_value=SimpleNamespace(hex="test")), \
             patch("integration.runpod_cpu_stager_smoke.SessionCoordinator", return_value=coordinator), \
             patch("integration.runpod_cpu_stager_smoke.RunPodLifecycleAdapter"), \
             patch("integration.runpod_cpu_stager_smoke.SessionLifecycleService", return_value=service), \
             patch("integration.runpod_cpu_stager_smoke.stage_models", side_effect=RuntimeError("stage failed")), \
             patch.dict(os.environ, {"RUNPOD_API_KEY": "api", "RUNONRUNPOD_CPU_STAGING_SIGNING_KEY": "hmac"}, clear=False), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(run(self._live_arguments()), 1)
        service.end.assert_called_once_with("smoke-test")
        self.assertIn("SMOKE TEST FAILED", errors.getvalue())

    @staticmethod
    def _live_arguments():
        return [
            "--live", "--data-center", "dc-1", "--cpu-template-id", "template-cpu",
            "--cpu-image", "image@sha256:abc", "--cpu-flavor-id", "cpu3c", "--vcpu-count", "4",
            "--hmac-secret-name", "hmac",
            "--provider-secret-name", "provider", "--download-url", "https://example.test/base",
            "--sha256", "a" * 64, "--size", "1", "--state-root", ".",
        ]


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
