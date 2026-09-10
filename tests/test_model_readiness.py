"""Hermetic volume-receipt tests without an S3 service."""

import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest

from resource_plan import ModelBinding, ModelIdentity, ModelRequirement


def load_readiness_module():
    root = Path(__file__).resolve().parents[1]
    package = ModuleType("_readiness_tests_plugin")
    package.__path__ = [str(root)]
    resource_spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.resource_plan", root / "resource_plan.py",
    )
    resource_module = importlib.util.module_from_spec(resource_spec)
    readiness_spec = importlib.util.spec_from_file_location(
        f"{package.__name__}.model_readiness", root / "model_readiness.py",
    )
    readiness_module = importlib.util.module_from_spec(readiness_spec)
    modules = {
        package.__name__: package,
        resource_spec.name: resource_module,
        readiness_spec.name: readiness_module,
    }
    previous = {name: sys.modules.get(name) for name in modules}
    try:
        sys.modules.update(modules)
        resource_spec.loader.exec_module(resource_module)
        readiness_spec.loader.exec_module(readiness_module)
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
    return readiness_module


class _Body:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class _Volume:
    def __init__(self):
        self.objects = {}

    def get_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = Body.read() if hasattr(Body, "read") else Body

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


class ModelReadinessTests(unittest.TestCase):
    def setUp(self):
        self.readiness = load_readiness_module()
        self.volume = _Volume()
        self.requirement = ModelRequirement(
            "checkpoints", "base.safetensors", "models/checkpoints/base.safetensors",
            (ModelBinding("1", "CheckpointLoaderSimple", "ckpt_name"),),
        )
        self.identity = ModelIdentity("a" * 64, 5)
        self.volume.objects[self.requirement.target_path] = b"12345"

    def test_receipt_is_only_ready_after_matching_object_size(self):
        self.readiness.write_receipt(
            self.volume, "volume", self.requirement, self.identity, "local-upload",
        )
        receipt = self.readiness.load_matching_receipt(
            self.volume, "volume", self.requirement,
        )
        self.assertEqual(receipt.identity.sha256, self.identity.sha256)
        self.assertEqual(receipt.identity.size, self.identity.size)
        self.volume.objects[self.requirement.target_path] = b"wrong-size"
        self.assertIsNone(self.readiness.load_matching_receipt(
            self.volume, "volume", self.requirement,
        ))

    def test_stale_receipt_is_cleared_before_replacement(self):
        self.readiness.write_receipt(
            self.volume, "volume", self.requirement, self.identity, "local-upload",
        )
        self.readiness.clear_receipt(self.volume, "volume", self.requirement)
        self.assertIsNone(self.readiness.load_matching_receipt(
            self.volume, "volume", self.requirement,
        ))

    def test_receipt_is_bound_to_its_exact_target_path(self):
        self.readiness.write_receipt(
            self.volume, "volume", self.requirement, self.identity, "local-upload",
        )
        other = ModelRequirement(
            "loras", "base.safetensors", "models/loras/base.safetensors", (),
        )
        receipt_key = self.readiness.receipt_key(self.requirement.target_path)
        self.volume.objects[self.readiness.receipt_key(other.target_path)] = self.volume.objects[receipt_key]
        self.volume.objects[other.target_path] = b"12345"
        self.assertIsNone(self.readiness.load_matching_receipt(self.volume, "volume", other))


if __name__ == "__main__":
    unittest.main()
