"""Hermetic transport tests for CPU staging submission and polling."""

import asyncio
import hashlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cpu_stager_client import CpuStagerError, stage_models
from cpu_staging_contract import sign_stage_request


class FakeResponse:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self):
        return self.value


class FakeSession:
    def __init__(self, submitted, statuses):
        self.submitted = submitted
        self.statuses = iter(statuses)
        self.posts = []
        self.gets = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse(self.submitted)

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse(next(self.statuses))


class CpuStagerClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        digest = hashlib.sha256(b"model").hexdigest()
        self.request = {
            "protocol_version": 1, "operation_id": "prep-1", "volume_binding": "volume-1",
            "models": [{
                "target_path": "models/checkpoints/base.safetensors", "url": "https://example.test/base",
                "expected_sha256": digest, "expected_size": 5, "auth": "none",
            }],
        }
        self.envelope = sign_stage_request(self.request, "key")
        self.result = {
            "protocol_version": 1, "operation_id": "prep-1", "status": "success",
            "results": [{
                "target_path": "models/checkpoints/base.safetensors", "status": "done",
                "sha256": digest, "size": 5,
            }],
        }

    async def test_submits_signed_envelope_reports_progress_and_validates_result(self):
        session = FakeSession({"id": "job-1"}, [
            {"status": "IN_PROGRESS", "output": {"results": []}},
            {"status": "COMPLETED", "output": self.result},
        ])
        progress = []
        aiohttp = SimpleNamespace(ClientSession=lambda: session)
        with patch.dict(sys.modules, {"aiohttp": aiohttp}), \
             patch("cpu_stager_client.asyncio.sleep", new=AsyncMock()) as sleep:
            result = await stage_models("cpu-endpoint", "api-key", self.envelope, progress.append)

        self.assertEqual(result, self.result)
        self.assertEqual(progress, [{"results": []}])
        self.assertEqual(session.posts[0][0], "https://api.runpod.ai/v2/cpu-endpoint/run")
        self.assertEqual(session.posts[0][1]["headers"], {
            "Authorization": "Bearer api-key", "User-Agent": "ComfyUI-RunOnRunpod",
        })
        self.assertEqual(session.gets[0][1]["headers"], {
            "Authorization": "Bearer api-key", "User-Agent": "ComfyUI-RunOnRunpod",
        })
        self.assertEqual(session.posts[0][1]["json"], {"input": {"signed_request": self.envelope}})
        self.assertEqual(len(session.gets), 2)
        self.assertEqual(sleep.await_count, 2)

    async def test_terminal_failure_is_reported_without_accepting_output(self):
        session = FakeSession({"id": "job-1"}, [{"status": "FAILED", "error": "download failed"}])
        aiohttp = SimpleNamespace(ClientSession=lambda: session)
        with patch.dict(sys.modules, {"aiohttp": aiohttp}), \
             patch("cpu_stager_client.asyncio.sleep", new=AsyncMock()):
            with self.assertRaisesRegex(CpuStagerError, "download failed"):
                await stage_models("cpu-endpoint", "api-key", self.envelope)

    async def test_timeout_stops_polling_before_another_status_request(self):
        session = FakeSession({"id": "job-1"}, [])
        aiohttp = SimpleNamespace(ClientSession=lambda: session)
        with patch.dict(sys.modules, {"aiohttp": aiohttp}), \
             patch("cpu_stager_client.time.monotonic", side_effect=(100, 102)):
            with self.assertRaisesRegex(CpuStagerError, "timed out"):
                await stage_models("cpu-endpoint", "api-key", self.envelope, timeout_seconds=1)
        self.assertEqual(session.gets, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
