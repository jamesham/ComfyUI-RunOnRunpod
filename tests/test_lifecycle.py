"""Hermetic lifecycle tests using a deterministic in-memory provider."""

import hashlib
from types import SimpleNamespace
import tempfile
import unittest

from coordinator import LifecycleError, ManagedProfile, SessionCoordinator, SessionLifecycleService
from resource_plan import ModelIdentity, compile_model_resource_plan


class FakeProvider:
    def __init__(self):
        self.resources = {}
        self.calls = []
        self.fail_volume = False

    def ensure_volume(self, profile, session_id):
        self.calls.append(("ensure_volume", session_id))
        if self.fail_volume:
            raise RuntimeError("capacity unavailable")
        return self.resources.setdefault(("volume", session_id), {
            "id": f"volume-{session_id}", "binding": f"binding-{session_id}", "owner": session_id,
        })

    def ensure_cpu_endpoint(self, profile, session_id, volume):
        self.calls.append(("ensure_cpu", session_id, volume["id"]))
        return self.resources.setdefault(("cpu_endpoint", session_id), {
            "id": f"cpu-{session_id}", "owner": session_id, "volume_id": volume["id"],
        })

    def ensure_gpu_endpoint(self, profile, session_id, volume):
        self.calls.append(("ensure_gpu", session_id, volume["id"]))
        return self.resources.setdefault(("gpu_endpoint", session_id), {
            "id": f"gpu-{session_id}", "owner": session_id, "volume_id": volume["id"],
        })

    def get_resource(self, resource, resource_id):
        for (kind, _session), value in self.resources.items():
            if kind == resource and value["id"] == resource_id:
                return value
        return None

    def delete_resource(self, resource, resource_id):
        self.calls.append(("delete", resource, resource_id))
        for key, value in list(self.resources.items()):
            if key[0] == resource and value["id"] == resource_id:
                del self.resources[key]
                return
        raise RuntimeError("unknown resource")


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.coordinator = SessionCoordinator(self.directory.name)
        self.provider = FakeProvider()
        self.service = SessionLifecycleService(self.coordinator, self.provider)
        self.profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc",
            gpu_image="gpu-image@sha256:def", gpu_pool_ids=("ADA_24",),
        )
        plan = compile_model_resource_plan({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }, {"CheckpointLoaderSimple": ("ckpt_name", "checkpoints")})
        requirement = plan.requirements[0]
        digest = hashlib.sha256(b"model").hexdigest()
        self.coordinator.save_recipe("recipe-1", plan, {
            requirement.target_path: SimpleNamespace(
                identity=ModelIdentity(digest, 5), local_path=None,
                descriptor={"url": "https://example.test/base", "auth": "none"},
            ),
        })

    def test_start_recover_and_close_use_exact_recorded_resources(self):
        started = self.service.start("recipe-1", self.profile, session_id="session-1")
        self.assertEqual(started["state"], "preparing")
        self.assertEqual(started["bindings"]["volume_binding"], "binding-session-1")
        self.assertEqual(started["bindings"]["cpu_endpoint_id"], "cpu-session-1")
        self.assertEqual(self.coordinator.get_profile("profile-1")["revision"], 1)

        recovered = self.service.recover("session-1", self.profile)
        self.assertEqual(recovered["bindings"]["volume_id"], "volume-session-1")
        self.assertEqual(self.provider.calls.count(("ensure_volume", "session-1")), 1)

        gpu_ready = self.service.ensure_gpu_endpoint("session-1", self.profile)
        self.assertEqual(gpu_ready["bindings"]["gpu_endpoint_id"], "gpu-session-1")
        reconciled_gpu = self.service.ensure_gpu_endpoint("session-1", self.profile)
        self.assertEqual(reconciled_gpu["bindings"]["gpu_endpoint_id"], "gpu-session-1")
        self.assertEqual(self.provider.calls.count(("ensure_gpu", "session-1", "volume-session-1")), 1)

        closed = self.service.end("session-1")
        self.assertEqual(closed["state"], "closed")
        deletes = [call for call in self.provider.calls if call[0] == "delete"]
        self.assertEqual([call[1] for call in deletes], ["gpu_endpoint", "cpu_endpoint", "volume"])
        self.assertEqual(self.provider.resources, {})

    def test_failed_provisioning_is_persisted_as_recoverable(self):
        self.provider.fail_volume = True
        with self.assertRaisesRegex(LifecycleError, "volume provisioning failed"):
            self.service.start("recipe-1", self.profile, session_id="session-1")
        session = self.coordinator.get_session("session-1")
        self.assertEqual(session["state"], "recoverable")
        failed = [entry for entry in session["operations"].values() if entry["state"] == "failed"]
        self.assertEqual(failed[0]["result"]["error"], "capacity unavailable")

    def test_profile_rejects_non_cpu_scaling_policy(self):
        with self.assertRaisesRegex(LifecycleError, "min_workers"):
            ManagedProfile.from_dict({
                "profile_version": 1, "profile_id": "p", "data_center": "dc",
                "volume": {"size_gb": 10},
                "cpu": {"image": "cpu", "min_workers": 1, "max_workers": 1},
            })

    def test_profile_parses_managed_gpu_policy_without_changing_schema_version(self):
        profile = ManagedProfile.from_dict({
            "profile_version": 1, "profile_id": "p", "data_center": "dc",
            "volume": {"size_gb": 10},
            "cpu": {"image": "cpu", "min_workers": 0, "max_workers": 1},
            "gpu": {
                "image": "gpu@sha256:def", "pool_ids": ["ADA_24", "AMPERE_80"],
                "count": 1, "disk_gb": 80, "min_workers": 0, "max_workers": 1,
                "idle_timeout_seconds": 7, "execution_timeout_ms": 900_000,
                "environment": {"SAFE_SETTING": "value"},
            },
        })
        self.assertEqual(profile.gpu_pool_ids, ("ADA_24", "AMPERE_80"))
        self.assertEqual(profile.to_dict()["gpu"]["disk_gb"], 80)
        self.assertEqual(profile.to_dict()["profile_version"], 1)

    def test_cpu_mode_gpu_profile_rejects_provider_credentials(self):
        with self.assertRaisesRegex(LifecycleError, "model-provider credentials"):
            ManagedProfile.from_dict({
                "profile_version": 1, "profile_id": "p", "data_center": "dc",
                "volume": {"size_gb": 10}, "cpu": {"image": "cpu"},
                "gpu": {
                    "image": "gpu", "pool_ids": ["ADA_24"],
                    "environment": {"HF_TOKEN": "{{ RUNPOD_SECRET_hf }}"},
                },
            })


if __name__ == "__main__":
    unittest.main()
