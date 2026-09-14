"""Hermetic tests for durable managed-session coordination."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest

from coordinator import CoordinatorError, SessionCoordinator
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

        request = self.coordinator.prepare_stage_request(
            "session-1", "prep-1", self.downloads,
        )
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
        self.coordinator.prepare_stage_request("session-1", "prep-1", self.downloads)
        changed = [dict(self.downloads[0], expected_size=12)]
        with self.assertRaisesRegex(CoordinatorError, "different content"):
            self.coordinator.prepare_stage_request("session-1", "prep-1", changed)

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

    def test_lifecycle_lock_serializes_the_whole_remote_operation_window(self):
        first_acquired = threading.Event()
        release_first = threading.Event()
        second_attempting = threading.Event()
        second_acquired = threading.Event()

        def first():
            with self.coordinator.lifecycle_lock("session-1"):
                first_acquired.set()
                release_first.wait(2)

        def second():
            second_attempting.set()
            with self.coordinator.lifecycle_lock("session-1"):
                second_acquired.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        first_thread.start()
        self.assertTrue(first_acquired.wait(1))
        second_thread.start()
        self.assertTrue(second_attempting.wait(1))
        self.assertFalse(second_acquired.wait(0.05))
        release_first.set()
        first_thread.join(2)
        second_thread.join(2)
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(second_thread.is_alive())
        self.assertTrue(second_acquired.is_set())


if __name__ == "__main__":
    unittest.main()
