"""Hermetic RunPod REST adapter tests; no provider credentials or calls."""

import hashlib
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from coordinator import (
    ManagedProfile,
    RunPodAdapterError,
    RunPodLifecycleAdapter,
    SessionCoordinator,
    SessionLifecycleService,
)
from coordinator.runpod_adapter import USER_AGENT
from resource_plan import ModelIdentity, compile_model_resource_plan


class FakeRunPodRest:
    def __init__(self):
        self.requests = []
        self.volumes = {}
        self.endpoints = {}
        self.bad_endpoint = False

    def __call__(self, method, path, payload):
        self.requests.append((method, path, payload))
        if method == "GET" and path == "/network-volumes":
            return 200, {"networkVolumes": list(self.volumes.values())}
        if method == "POST" and path == "/network-volumes":
            resource = {
                "id": f"vol-{len(self.volumes) + 1}", "name": payload["name"],
                "size": payload["size"], "dataCenter": payload["dataCenter"], "type": "STANDARD",
            }
            self.volumes[resource["id"]] = resource
            return 201, resource
        if method == "DELETE" and path.startswith("/network-volumes/"):
            self.volumes.pop(path.rsplit("/", 1)[1], None)
            return 204, None
        if method == "GET" and path == "/serverless":
            return 200, {"endpoints": list(self.endpoints.values())}
        if method == "POST" and path == "/serverless":
            compute = "cpu" if "cpu" in payload else "gpu"
            resource = {
                "id": f"{compute}-{len(self.endpoints) + 1}", "name": payload["name"],
                "type": "LOAD_BALANCER" if self.bad_endpoint else payload["type"],
                "networkVolumes": payload["networkVolumes"],
                "workers": payload["workers"], "scaling": payload["scaling"],
                "timeout": payload["timeout"],
            }
            if compute == "cpu":
                resource["cpu"] = payload["cpu"]
            else:
                resource.update({
                    "image": payload["image"], "gpu": payload["gpu"], "disk": payload["disk"],
                })
            self.endpoints[resource["id"]] = resource
            return 201, resource
        if method == "GET" and path.startswith("/serverless/"):
            resource = self.endpoints.get(path.rsplit("/", 1)[1])
            return (200, resource) if resource is not None else (404, {"title": "Not Found", "status": 404})
        if method == "DELETE" and path.startswith("/serverless/"):
            self.endpoints.pop(path.rsplit("/", 1)[1], None)
            return 204, None
        return 500, {"error": f"unexpected request {method} {path}"}


class RunPodAdapterTests(unittest.TestCase):
    def setUp(self):
        self.rest = FakeRunPodRest()
        self.profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc",
            gpu_image="gpu-image@sha256:def",
            cpu_template_id="template-cpu", cpu_flavor_ids=("cpu3c",), cpu_vcpu_count=4,
            gpu_pool_ids=("ADA_24", "AMPERE_80"), gpu_disk_gb=80,
            gpu_idle_timeout_seconds=7, gpu_execution_timeout_ms=900_000,
        )

    def adapter(self, *, mutations=True):
        return RunPodLifecycleAdapter("test-key", allow_mutations=mutations, transport=self.rest)

    def test_http_transport_sets_an_explicit_lifecycle_user_agent(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"[]"

        adapter = RunPodLifecycleAdapter("test-key")
        with patch("coordinator.runpod_adapter.urllib.request.urlopen", return_value=Response()) as urlopen:
            status, value = adapter._http("GET", "/network-volumes", None)

        request = urlopen.call_args.args[0]
        self.assertEqual((status, value), (200, []))
        self.assertEqual(request.full_url, "https://api.runpod.io/v2/network-volumes")
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertNotIn("Python-urllib", request.get_header("User-agent"))

    def test_mutating_calls_are_disabled_by_default(self):
        with self.assertRaisesRegex(RunPodAdapterError, "mutations are disabled"):
            self.adapter(mutations=False).ensure_volume(self.profile, "session-1")
        self.assertEqual([request[:2] for request in self.rest.requests], [("GET", "/network-volumes")])

    def test_debug_callback_receives_each_lifecycle_request_and_result(self):
        calls = []
        adapter = RunPodLifecycleAdapter(
            "test-key", allow_mutations=True, transport=self.rest,
            debug=lambda *call: calls.append(call),
        )
        adapter.ensure_volume(self.profile, "session-1")

        self.assertEqual([(call[0], call[1], call[3]) for call in calls], [
            ("GET", "/network-volumes", 200),
            ("POST", "/network-volumes", 201),
        ])
        self.assertEqual(calls[1][2], {
            "name": "runonrunpod-volume-session-1", "size": 100, "dataCenter": "dc-1",
        })
        self.assertEqual(calls[1][4]["id"], "vol-1")

    def test_creates_cpu_endpoint_with_volume_and_cpu_limits(self):
        adapter = self.adapter()
        volume = adapter.ensure_volume(self.profile, "session-1")
        endpoint = adapter.ensure_cpu_endpoint(self.profile, "session-1", volume)
        request = self.rest.requests[-1]
        self.assertEqual(request[:2], ("POST", "/serverless"))
        self.assertEqual(request[2], {
            "name": "runonrunpod-cpu-session-1", "templateId": "template-cpu",
            "type": "QUEUE", "cpu": [{"id": "cpu3c", "vcpuCount": 4}],
            "dataCenterIds": ["dc-1"], "networkVolumes": ["vol-1"],
            "workers": {"min": 0, "max": 1, "idleTimeout": 5},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4},
            "timeout": 3_600_000, "env": {"STAGING_VOLUME_BINDING": "vol-1"},
        })
        self.assertEqual(endpoint["volume_id"], "vol-1")
        self.assertEqual(endpoint["ownership"]["name"], "runonrunpod-cpu-session-1")

    def test_endpoint_environment_keeps_secret_references_and_sets_volume_binding(self):
        profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc", cpu_template_id="template-cpu",
            cpu_flavor_ids=("cpu3c",), cpu_vcpu_count=4,
            cpu_environment=(("HF_TOKEN", "{{ RUNPOD_SECRET_hf_test }}"),),
        )
        adapter = self.adapter()
        volume = adapter.ensure_volume(profile, "session-1")
        adapter.ensure_cpu_endpoint(profile, "session-1", volume)
        self.assertEqual(self.rest.requests[-1][2]["env"], {
            "HF_TOKEN": "{{ RUNPOD_SECRET_hf_test }}", "STAGING_VOLUME_BINDING": "vol-1",
        })

    def test_creates_gpu_endpoint_on_same_volume_with_scale_to_zero_policy(self):
        adapter = self.adapter()
        volume = adapter.ensure_volume(self.profile, "session-1")
        endpoint = adapter.ensure_gpu_endpoint(self.profile, "session-1", volume)
        self.assertEqual(self.rest.requests[-1], ("POST", "/serverless", {
            "name": "runonrunpod-gpu-session-1", "image": "gpu-image@sha256:def",
            "type": "QUEUE", "gpu": {"pools": ["ADA_24", "AMPERE_80"], "count": 1},
            "disk": 80, "dataCenterIds": ["dc-1"], "networkVolumes": ["vol-1"],
            "workers": {"min": 0, "max": 1, "idleTimeout": 7},
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4},
            "timeout": 900_000, "env": {},
        }))
        self.assertEqual(endpoint["compute_type"], "GPU")
        self.assertEqual(endpoint["volume_id"], "vol-1")

    def test_rejects_conflicting_configured_volume_binding(self):
        profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc", cpu_template_id="template-cpu",
            cpu_flavor_ids=("cpu3c",), cpu_vcpu_count=4,
            cpu_environment=(("STAGING_VOLUME_BINDING", "other-volume"),),
        )
        adapter = self.adapter()
        volume = adapter.ensure_volume(profile, "session-1")
        with self.assertRaisesRegex(RunPodAdapterError, "binding conflicts"):
            adapter.ensure_cpu_endpoint(profile, "session-1", volume)

    def test_refuses_matching_name_without_recorded_ownership(self):
        self.rest.volumes["outside-volume"] = {
            "id": "outside-volume", "name": "runonrunpod-volume-session-1",
            "size": 100, "dataCenter": "dc-1", "type": "STANDARD",
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
        service.ensure_gpu_endpoint("session-1", self.profile)
        self.rest.requests.clear()

        recovered = service.recover("session-1", self.profile)
        self.assertEqual(recovered["state"], "ready")
        self.assertFalse(any(request[0] == "POST" for request in self.rest.requests))
        self.assertEqual([request[:2] for request in self.rest.requests], [
            ("GET", "/network-volumes"), ("GET", "/serverless/cpu-1"),
            ("GET", "/serverless/gpu-2"),
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

    def test_rejects_invalid_gpu_response(self):
        self.rest.bad_endpoint = True
        adapter = self.adapter()
        volume = adapter.ensure_volume(self.profile, "session-1")
        with self.assertRaisesRegex(RunPodAdapterError, "compute/volume binding"):
            adapter.ensure_gpu_endpoint(self.profile, "session-1", volume)

    def test_rejects_cpu_endpoint_without_the_explicit_v2_cpu_configuration(self):
        profile = ManagedProfile(
            "profile-1", "dc-1", 100, "cpu-image@sha256:abc", cpu_template_id="template-cpu",
        )
        volume = self.adapter().ensure_volume(profile, "session-1")
        with self.assertRaisesRegex(RunPodAdapterError, "requires cpu_flavor_ids and cpu_vcpu_count"):
            self.adapter().ensure_cpu_endpoint(profile, "session-1", volume)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
