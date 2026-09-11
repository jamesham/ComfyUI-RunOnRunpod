"""Hermetic RunPod REST adapter tests; no provider credentials or calls."""

import hashlib
from types import SimpleNamespace
import tempfile
import unittest

from coordinator import (
    ManagedProfile,
    RunPodAdapterError,
    RunPodLifecycleAdapter,
    SessionCoordinator,
    SessionLifecycleService,
)
from resource_plan import ModelIdentity, compile_model_resource_plan


class FakeRunPodRest:
    def __init__(self):
        self.requests = []
        self.volumes = {}
        self.endpoints = {}
        self.bad_endpoint = False

    def __call__(self, method, path, payload):
        self.requests.append((method, path, payload))
        if method == "GET" and path == "/networkvolumes":
            return 200, list(self.volumes.values())
        if method == "POST" and path == "/networkvolumes":
            resource = {
                "id": f"vol-{len(self.volumes) + 1}", "name": payload["name"],
                "size": payload["size"], "dataCenterId": payload["dataCenterId"],
            }
            self.volumes[resource["id"]] = resource
            return 201, resource
        if method == "DELETE" and path.startswith("/networkvolumes/"):
            self.volumes.pop(path.rsplit("/", 1)[1], None)
            return 204, None
        if method == "GET" and path == "/endpoints":
            return 200, list(self.endpoints.values())
        if method == "POST" and path == "/endpoints":
            resource = {
                "id": f"cpu-{len(self.endpoints) + 1}", "name": payload["name"],
                "computeType": "GPU" if self.bad_endpoint else payload["computeType"],
                "networkVolumeId": payload["networkVolumeId"],
                "workersMin": payload["workersMin"], "workersMax": payload["workersMax"],
            }
            self.endpoints[resource["id"]] = resource
            return 201, resource
        if method == "GET" and path.startswith("/endpoints/"):
            resource = self.endpoints.get(path.rsplit("/", 1)[1])
            return (200, resource) if resource is not None else (404, {"error": "not found"})
        if method == "DELETE" and path.startswith("/endpoints/"):
            self.endpoints.pop(path.rsplit("/", 1)[1], None)
            return 204, None
        return 500, {"error": f"unexpected request {method} {path}"}


class RunPodAdapterTests(unittest.TestCase):
    def setUp(self):
        self.rest = FakeRunPodRest()
        self.profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc",
            cpu_template_id="template-cpu", cpu_flavor_ids=("cpu3c",), cpu_vcpu_count=4,
        )

    def adapter(self, *, mutations=True):
        return RunPodLifecycleAdapter("test-key", allow_mutations=mutations, transport=self.rest)

    def test_mutating_calls_are_disabled_by_default(self):
        with self.assertRaisesRegex(RunPodAdapterError, "mutations are disabled"):
            self.adapter(mutations=False).ensure_volume(self.profile, "session-1")
        self.assertEqual([request[:2] for request in self.rest.requests], [("GET", "/networkvolumes")])

    def test_creates_cpu_endpoint_with_volume_and_cpu_limits(self):
        adapter = self.adapter()
        volume = adapter.ensure_volume(self.profile, "session-1")
        endpoint = adapter.ensure_cpu_endpoint(self.profile, "session-1", volume)
        request = self.rest.requests[-1]
        self.assertEqual(request[:2], ("POST", "/endpoints"))
        self.assertEqual(request[2], {
            "name": "runonrunpod-cpu-session-1", "templateId": "template-cpu",
            "computeType": "CPU", "dataCenterIds": ["dc-1"], "networkVolumeId": "vol-1",
            "workersMin": 0, "workersMax": 1, "idleTimeout": 5,
            "executionTimeoutMs": 3_600_000, "env": {"STAGING_VOLUME_BINDING": "vol-1"},
            "cpuFlavorIds": ["cpu3c"], "vcpuCount": 4,
        })
        self.assertEqual(endpoint["volume_id"], "vol-1")
        self.assertEqual(endpoint["ownership"]["name"], "runonrunpod-cpu-session-1")

    def test_endpoint_environment_keeps_secret_references_and_sets_volume_binding(self):
        profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc", cpu_template_id="template-cpu",
            cpu_environment=(("HF_TOKEN", "{{ RUNPOD_SECRET_hf_test }}"),),
        )
        adapter = self.adapter()
        volume = adapter.ensure_volume(profile, "session-1")
        adapter.ensure_cpu_endpoint(profile, "session-1", volume)
        self.assertEqual(self.rest.requests[-1][2]["env"], {
            "HF_TOKEN": "{{ RUNPOD_SECRET_hf_test }}", "STAGING_VOLUME_BINDING": "vol-1",
        })

    def test_rejects_conflicting_configured_volume_binding(self):
        profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc", cpu_template_id="template-cpu",
            cpu_environment=(("STAGING_VOLUME_BINDING", "other-volume"),),
        )
        adapter = self.adapter()
        volume = adapter.ensure_volume(profile, "session-1")
        with self.assertRaisesRegex(RunPodAdapterError, "binding conflicts"):
            adapter.ensure_cpu_endpoint(profile, "session-1", volume)

    def test_refuses_matching_name_without_recorded_ownership(self):
        self.rest.volumes["outside-volume"] = {
            "id": "outside-volume", "name": "runonrunpod-volume-session-1",
            "size": 100, "dataCenterId": "dc-1",
        }
        with self.assertRaisesRegex(RunPodAdapterError, "refuse unsafe reuse"):
            self.adapter().ensure_volume(self.profile, "session-1")
        self.assertFalse(any(request[0] == "POST" for request in self.rest.requests))

    def test_recovery_reuses_recorded_resources_then_closes_them(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        coordinator = SessionCoordinator(directory.name)
        plan = compile_model_resource_plan({
            "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "base.safetensors"}},
        }, {"CheckpointLoaderSimple": ("ckpt_name", "checkpoints")})
        requirement = plan.requirements[0]
        coordinator.save_recipe("recipe-1", plan, {
            requirement.target_path: SimpleNamespace(
                identity=ModelIdentity(hashlib.sha256(b"model").hexdigest(), 5), local_path=None,
                descriptor={"url": "https://example.test/base", "auth": "none"},
            ),
        })
        service = SessionLifecycleService(coordinator, self.adapter())
        service.start("recipe-1", self.profile, session_id="session-1")
        self.rest.requests.clear()

        recovered = service.recover("session-1", self.profile)
        self.assertEqual(recovered["state"], "preparing")
        self.assertFalse(any(request[0] == "POST" for request in self.rest.requests))
        self.assertEqual([request[:2] for request in self.rest.requests], [
            ("GET", "/networkvolumes"), ("GET", "/endpoints/cpu-1"),
        ])

        closed = service.end("session-1")
        self.assertEqual(closed["state"], "closed")
        self.assertEqual(self.rest.volumes, {})
        self.assertEqual(self.rest.endpoints, {})

    def test_rejects_invalid_cpu_response(self):
        self.rest.bad_endpoint = True
        adapter = self.adapter()
        volume = adapter.ensure_volume(self.profile, "session-1")
        with self.assertRaisesRegex(RunPodAdapterError, "compute/volume binding"):
            adapter.ensure_cpu_endpoint(self.profile, "session-1", volume)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
