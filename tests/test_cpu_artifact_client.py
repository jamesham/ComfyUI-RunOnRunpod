"""Hermetic RunPod transport tests for CPU artifact chunks."""

import hashlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cpu_artifact_client import upload_file


class FakeResponse:
    status = 200

    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.value


class FakeSession:
    def __init__(self, submitted, output):
        self.submitted = submitted
        self.output = output
        self.posts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.submitted)

    def get(self, _url, **_kwargs):
        output = dict(self.output)
        payload = self.posts[-1][1]["json"]["input"]["signed_artifact_request"]["payload"]
        output["operation_id"] = payload["operation_id"]
        output["target_path"] = payload["target_path"]
        output["action"] = payload["action"]
        return FakeResponse({"status": "COMPLETED", "output": output})


class CpuArtifactClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_posts_signed_chunks_without_s3(self):
        content = b"small local asset"
        digest = hashlib.sha256(content).hexdigest()
        result = {
            "artifact_protocol_version": 1, "operation_id": "prep-1", "status": "success",
            "action": "write", "target_path": "models/checkpoints/model.bin",
            "sha256": digest, "size": len(content),
        }
        session = FakeSession({"id": "job-1"}, result)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "model.bin"
            source.write_bytes(content)
            aiohttp = SimpleNamespace(ClientSession=lambda: session)
            with patch.dict(sys.modules, {"aiohttp": aiohttp}), \
                 patch("cpu_artifact_client.asyncio.sleep", new=AsyncMock()):
                returned = await upload_file(
                    "cpu-endpoint", "api-key", "hmac", "volume-1", "prep-1",
                    "models/checkpoints/model.bin", str(source),
                )
        self.assertEqual(returned, result)
        self.assertEqual(session.posts[0][0], "https://api.runpod.ai/v2/cpu-endpoint/run")
        request = session.posts[0][1]["json"]["input"]["signed_artifact_request"]
        self.assertEqual(request["payload"]["action"], "write")
        self.assertEqual(request["payload"]["target_path"], "models/checkpoints/model.bin")
        self.assertEqual(request["payload"]["expected_sha256"], digest)
        self.assertEqual(session.posts[0][1]["headers"]["User-Agent"], "ComfyUI-RunOnRunpod")
        # The client signs the payload locally and never sends a provider token
        # or an S3 bucket/credential field.
        self.assertNotIn("s3", repr(session.posts[0][1]["json"]).lower())
