"""Hermetic tests for durable managed-session coordination."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from coordinator import CoordinatorError, SessionCoordinator
from cpu_staging_contract import verify_signed_stage_request
from resource_plan import ModelIdentity, compile_model_resource_plan


FIELDS = {"CheckpointLoaderSimple": ("ckpt_name", "checkpoints")}


class SessionCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.coordinator = SessionCoordinator(self.directory.name)
        self.plan = compile_model_resource_plan({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }, FIELDS)
        self.sha256 = hashlib.sha256(b"model bytes").hexdigest()
        requirement = self.plan.requirements[0]
        self.materialized = {
            requirement.target_path: SimpleNamespace(
                identity=ModelIdentity(self.sha256, 11),
                local_path=None,
                descriptor={
                    "url": "https://example.test/base.safetensors", "auth": "none",
                },
            ),
        }
        self.downloads = [{
            "dest_path": requirement.target_path,
            "url": "https://example.test/base.safetensors", "auth": "none",
            "expected_sha256": self.sha256, "expected_size": 11,
        }]

    def test_recipe_session_authorization_and_completion_are_durable(self):
        recipe = self.coordinator.save_recipe("recipe-1", self.plan, self.materialized)
        self.assertEqual(recipe["revision"], 1)
        session = self.coordinator.create_session(
            "recipe-1", session_id="session-1", volume_binding="volume-binding",
            cpu_endpoint_id="cpu-endpoint", gpu_endpoint_id="gpu-endpoint",
        )
        self.assertEqual(session["state"], "planned")

        envelope = self.coordinator.authorize_stage(
            "session-1", "prep-1", self.downloads, "coordinator-key",
        )
        request = verify_signed_stage_request(envelope, "coordinator-key")
        self.assertEqual(request["volume_binding"], "volume-binding")
        persisted = self.coordinator.get_session("session-1")
        self.assertEqual(persisted["state"], "preparing")
        self.assertNotIn("coordinator-key", str(persisted))

        result = {
            "protocol_version": 1, "operation_id": "prep-1", "status": "success",
            "results": [{
                "target_path": "models/checkpoints/base.safetensors", "status": "done",
                "sha256": self.sha256, "size": 11,
            }],
        }
        completed = self.coordinator.record_stage_result("session-1", "prep-1", result)
        self.assertEqual(completed["state"], "ready")
        self.assertEqual(completed["preparations"]["prep-1"]["state"], "ready")
        self.assertTrue((Path(self.directory.name) / "sessions/session-1/session.json").is_file())

    def test_same_preparation_id_cannot_authorize_different_content(self):
        self.coordinator.save_recipe("recipe-1", self.plan, self.materialized)
        self.coordinator.create_session(
            "recipe-1", session_id="session-1", volume_binding="volume-binding",
            cpu_endpoint_id="cpu-endpoint",
        )
        self.coordinator.authorize_stage("session-1", "prep-1", self.downloads, "key")
        changed = [dict(self.downloads[0], expected_size=12)]
        with self.assertRaisesRegex(CoordinatorError, "different content"):
            self.coordinator.authorize_stage("session-1", "prep-1", changed, "key")

    def test_local_recipe_source_requires_portable_reference(self):
        requirement = self.plan.requirements[0]
        local = {
            requirement.target_path: SimpleNamespace(
                identity=ModelIdentity(self.sha256, 11), local_path="/machine/model", descriptor=None,
            ),
        }
        with self.assertRaisesRegex(CoordinatorError, "relative path"):
            self.coordinator.save_recipe("recipe-1", self.plan, local)
        recipe = self.coordinator.save_recipe(
            "recipe-1", self.plan, local,
            local_references={requirement.target_path: "private-models/base.safetensors"},
        )
        self.assertEqual(recipe["models"][0]["source"], {
            "kind": "local", "reference": "private-models/base.safetensors",
        })


if __name__ == "__main__":
    unittest.main()
