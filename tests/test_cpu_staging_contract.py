"""Hermetic tests for the coordinator-to-CPU-stager boundary."""

import hashlib
import unittest

from cpu_staging_contract import (
    CpuStagingContractError,
    sign_stage_request,
    stage_request_from_downloads,
    validate_stage_result,
    verify_signed_stage_request,
)


class CpuStagingContractTests(unittest.TestCase):
    def setUp(self):
        self.sha256 = hashlib.sha256(b"model bytes").hexdigest()
        self.request = stage_request_from_downloads("prep-1", "volume-abc", [{
            "target_path": "models/checkpoints/base.safetensors",
            "url": "https://example.test/base.safetensors",
            "expected_sha256": self.sha256,
            "expected_size": 11,
            "auth": "none",
        }])

    def test_signed_request_is_deterministic_and_tamper_evident(self):
        envelope = sign_stage_request(self.request, "shared-secret")
        self.assertEqual(verify_signed_stage_request(envelope, "shared-secret"), self.request)
        envelope["payload"]["volume_binding"] = "different-volume"
        with self.assertRaisesRegex(CpuStagingContractError, "signature"):
            verify_signed_stage_request(envelope, "shared-secret")

    def test_result_requires_every_signed_target_and_identity(self):
        complete = {
            "protocol_version": 1, "operation_id": "prep-1", "status": "success",
            "results": [{
                "target_path": "models/checkpoints/base.safetensors", "status": "done",
                "sha256": self.sha256, "size": 11,
            }],
        }
        self.assertEqual(validate_stage_result(complete, self.request)["status"], "success")
        complete["results"][0]["size"] = 12
        with self.assertRaisesRegex(CpuStagingContractError, "identity mismatch"):
            validate_stage_result(complete, self.request)

    def test_request_rejects_non_model_targets_and_missing_identity(self):
        bad = dict(self.request)
        bad["models"] = [{
            "target_path": "outputs/escape", "url": "https://example.test/a",
            "expected_sha256": self.sha256, "expected_size": 11, "auth": "none",
        }]
        with self.assertRaisesRegex(CpuStagingContractError, "under models"):
            sign_stage_request(bad, "shared-secret")


if __name__ == "__main__":
    unittest.main()
