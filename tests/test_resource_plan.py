"""Hermetic tests for the workflow model resource-plan compiler."""

import hashlib
from pathlib import Path
import tempfile
import unittest

from resource_plan import (
    RESOURCE_PLAN_VERSION,
    ResourcePlanError,
    compile_model_resource_plan,
    materialize_model_requirement,
    model_resource_plan_dict,
    model_resource_plan_sha256,
)


FIELDS = {
    "CheckpointLoaderSimple": ("ckpt_name", "checkpoints"),
    "DualLoader": [("left", "loras"), ("right", "loras")],
}


class ResourcePlanTests(unittest.TestCase):
    def compile(self, workflow):
        return compile_model_resource_plan(workflow, FIELDS)

    def test_collects_exact_targets_and_every_binding(self):
        plan = self.compile({
            "10": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base/model.safetensors"}},
            "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base/model.safetensors"}},
            "3": {"class_type": "DualLoader", "inputs": {"left": "style.safetensors", "right": "detail.safetensors"}},
            "4": {"class_type": "Unknown", "inputs": {"filename": "ignored"}},
        })
        self.assertEqual(plan.version, RESOURCE_PLAN_VERSION)
        self.assertEqual([item.target_path for item in plan.requirements], [
            "models/checkpoints/base/model.safetensors",
            "models/loras/detail.safetensors",
            "models/loras/style.safetensors",
        ])
        checkpoint = plan.requirements[0]
        self.assertEqual([(binding.node_id, binding.input_name) for binding in checkpoint.bindings], [
            ("10", "ckpt_name"), ("2", "ckpt_name"),
        ])

    def test_serialized_plan_has_no_local_source_or_credential_fields(self):
        plan = self.compile({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        })
        self.assertEqual(model_resource_plan_dict(plan), {
            "resource_plan_version": 1,
            "models": [{
                "target_path": "models/checkpoints/base.safetensors",
                "bindings": [{"node_id": "1", "class_type": "CheckpointLoaderSimple", "input_name": "ckpt_name"}],
            }],
        })

    def test_hash_is_stable_for_equivalent_workflow_data(self):
        first = self.compile({"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}}})
        second = self.compile({"1": {"inputs": {"ckpt_name": "base.safetensors"}, "class_type": "CheckpointLoaderSimple"}})
        self.assertEqual(model_resource_plan_sha256(first), model_resource_plan_sha256(second))

    def test_unknown_nodes_and_non_filename_inputs_do_not_invent_requirements(self):
        plan = self.compile({
            "1": {"class_type": "Unknown", "inputs": {"name": "model.safetensors"}},
            "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": ["upstream", 0]}},
            "3": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
        })
        self.assertEqual(plan.requirements, ())

    def test_rejects_unsafe_model_filenames(self):
        for filename in ("", "/absolute.safetensors", "../escape.safetensors", "dir/../escape.safetensors", "dir//model.safetensors", r"dir\\model.safetensors", "C:/model.safetensors", "model\0.safetensors"):
            with self.subTest(filename=filename):
                with self.assertRaises(ResourcePlanError):
                    self.compile({"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": filename}}})

    def test_rejects_case_insensitive_target_collisions(self):
        with self.assertRaisesRegex(ResourcePlanError, "case-insensitive collision"):
            self.compile({
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "Model.safetensors"}},
                "2": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
            })

    def test_rejects_non_object_workflow_or_non_string_node_id(self):
        with self.assertRaises(ResourcePlanError):
            self.compile([])
        with self.assertRaises(ResourcePlanError):
            self.compile({1: {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}}})

    def test_remote_source_requires_complete_immutable_identity(self):
        requirement = self.compile({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }).requirements[0]
        descriptor = {
            "url": "https://example.test/base.safetensors",
            "dest_path": requirement.target_path,
            "expected_sha256": hashlib.sha256(b"remote").hexdigest(),
        }
        self.assertIsNone(materialize_model_requirement(requirement, None, descriptor))
        descriptor["expected_size"] = 6
        result = materialize_model_requirement(requirement, None, descriptor)
        self.assertIsNotNone(result)
        self.assertEqual(result.identity.size, 6)

    def test_local_file_identity_is_used_when_remote_claim_does_not_match(self):
        requirement = self.compile({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }).requirements[0]
        with tempfile.TemporaryDirectory() as directory:
            local_path = Path(directory) / "base.safetensors"
            local_path.write_bytes(b"local bytes")
            result = materialize_model_requirement(requirement, str(local_path), {
                "url": "https://example.test/base.safetensors",
                "dest_path": requirement.target_path,
                "expected_sha256": hashlib.sha256(b"other bytes").hexdigest(),
                "expected_size": 11,
            })
        self.assertIsNotNone(result)
        self.assertIsNone(result.descriptor)
        self.assertEqual(result.identity.sha256, hashlib.sha256(b"local bytes").hexdigest())


if __name__ == "__main__":
    unittest.main()
