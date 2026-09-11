"""Unit coverage for the live CPU-stager harness configuration only."""

from types import SimpleNamespace
import unittest

from integration.runpod_cpu_stager_smoke import _profile, _secret_reference


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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
