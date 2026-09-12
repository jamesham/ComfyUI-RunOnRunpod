"""Hermetic validation coverage for CPU-mode S3-free artifact requests."""

import base64
import hashlib
import unittest

from cpu_artifact_contract import (
    CpuArtifactContractError,
    sign_artifact_request,
    verify_signed_artifact_request,
)


class CpuArtifactContractTests(unittest.TestCase):
    def setUp(self):
        self.content = b"local model bytes"
        self.request = {
            "protocol_version": 1, "operation_id": "prep-1", "volume_binding": "volume-1",
            "action": "write", "target_path": "models/checkpoints/local.safetensors",
            "transfer_id": "transfer-1", "offset": 0,
            "data": base64.b64encode(self.content).decode("ascii"), "complete": True,
            "expected_sha256": hashlib.sha256(self.content).hexdigest(), "expected_size": len(self.content),
        }

    def test_signed_write_is_tamper_evident(self):
        envelope = sign_artifact_request(self.request, "key")
        self.assertEqual(verify_signed_artifact_request(envelope, "key"), self.request)
        envelope["payload"]["target_path"] = "outputs/other.png"
        with self.assertRaisesRegex(CpuArtifactContractError, "signature is invalid"):
            verify_signed_artifact_request(envelope, "key")

    def test_rejects_escape_and_oversized_reads(self):
        request = dict(self.request, target_path="models/../escape", action="read", offset=0, length=1)
        for key in ("transfer_id", "data", "complete", "expected_sha256", "expected_size"):
            request.pop(key)
        with self.assertRaisesRegex(CpuArtifactContractError, "safe relative path"):
            sign_artifact_request(request, "key")
        request["target_path"] = "outputs/file.png"
        request["length"] = 5 * 1024 * 1024
        with self.assertRaisesRegex(CpuArtifactContractError, "permitted range"):
            sign_artifact_request(request, "key")
