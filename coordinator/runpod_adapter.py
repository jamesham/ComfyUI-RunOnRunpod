"""RunPod REST v2 implementation of the lifecycle provider interface.

Mutating REST calls are disabled unless ``allow_mutations`` is explicitly set.
This keeps the adapter safe to construct in the ComfyUI server before an
operator has authorized paid resource creation.
"""

from __future__ import annotations

import json
from typing import Callable, Mapping
import urllib.error
import urllib.request

from .lifecycle import LifecycleError, ManagedProfile


REST_BASE = "https://api.runpod.io/v2"
USER_AGENT = "ComfyUI-RunOnRunpod-ManagedLifecycle/0.3.1"


class RunPodAdapterError(LifecycleError):
    pass


Transport = Callable[[str, str, Mapping[str, object] | None], tuple[int, object]]
DebugCall = Callable[[str, str, object, int | None, object], None]


class RunPodLifecycleAdapter:
    """Small, testable REST adapter; it performs no calls at construction."""

    def __init__(
        self,
        api_key: str,
        *,
        allow_mutations: bool = False,
        transport: Transport | None = None,
        debug: DebugCall | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise RunPodAdapterError("RunPod API key is required")
        self.api_key = api_key
        self.allow_mutations = allow_mutations
        self._transport = transport or self._http
        self._debug = debug

    def _http(self, method: str, path: str, payload: Mapping[str, object] | None) -> tuple[int, object]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            f"{REST_BASE}{path}", data=body, method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return response.status, json.loads(raw.decode("utf-8")) if raw else None
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                detail = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                detail = raw.decode("utf-8", errors="replace")
            return error.code, detail
        except urllib.error.URLError as error:
            raise RunPodAdapterError(f"RunPod REST transport error: {error.reason}") from None

    def _call(self, method: str, path: str, payload: Mapping[str, object] | None = None) -> object:
        if method in {"POST", "PATCH", "DELETE"} and not self.allow_mutations:
            raise RunPodAdapterError("RunPod lifecycle mutations are disabled")
        try:
            status, value = self._transport(method, path, payload)
        except Exception as error:
            if self._debug:
                self._debug(method, path, payload, None, {"error_type": type(error).__name__})
            raise
        if self._debug:
            self._debug(method, path, payload, status, value)
        if status < 200 or status >= 300:
            raise RunPodAdapterError(f"RunPod {method} {path} failed with HTTP {status}: {value}")
        return value

    @staticmethod
    def _name(session_id: str, resource: str) -> str:
        return f"runonrunpod-{resource}-{session_id}"

    @staticmethod
    def _single_named(items: object, name: str, resource: str) -> Mapping[str, object] | None:
        if not isinstance(items, list):
            raise RunPodAdapterError(f"RunPod returned invalid {resource} list")
        matches = [item for item in items if isinstance(item, Mapping) and item.get("name") == name]
        if len(matches) > 1:
            raise RunPodAdapterError(f"ambiguous existing {resource} name; manual reconciliation required")
        return matches[0] if matches else None

    def _list_resources(self, path: str, response_key: str, resource: str) -> list[object]:
        """Unwrap a REST v2 collection response before lifecycle matching."""
        value = self._call("GET", path)
        if not isinstance(value, Mapping) or not isinstance(value.get(response_key), list):
            raise RunPodAdapterError(f"RunPod returned invalid {resource} list response")
        return value[response_key]

    @staticmethod
    def _cpu_configurations(profile: ManagedProfile) -> list[dict[str, object]]:
        if not profile.cpu_flavor_ids or profile.cpu_vcpu_count is None:
            raise RunPodAdapterError(
                "RunPod REST v2 CPU endpoint creation requires cpu_flavor_ids and cpu_vcpu_count"
            )
        vcpu_count = profile.cpu_vcpu_count
        if vcpu_count < 2 or vcpu_count & (vcpu_count - 1):
            raise RunPodAdapterError("RunPod REST v2 CPU vcpu_count must be a power of two and at least 2")
        return [{"id": flavor_id, "vcpuCount": vcpu_count} for flavor_id in profile.cpu_flavor_ids]

    def ensure_volume(self, profile: ManagedProfile, session_id: str) -> Mapping[str, object]:
        name = self._name(session_id, "volume")
        existing = self._single_named(
            self._list_resources("/network-volumes", "networkVolumes", "volume"), name, "volume",
        )
        if existing is not None:
            # A name is not sufficient ownership proof after an uncertain create.
            raise RunPodAdapterError("matching volume name is ambiguous; refuse unsafe reuse")
        created = self._call("POST", "/network-volumes", {
            "name": name, "size": profile.volume_size_gb, "dataCenter": profile.data_center,
        })
        if not isinstance(created, Mapping) or not isinstance(created.get("id"), str):
            raise RunPodAdapterError("RunPod returned invalid volume creation response")
        if created.get("name") != name or created.get("dataCenter") != profile.data_center:
            raise RunPodAdapterError("RunPod volume response does not match requested ownership evidence")
        return {
            "id": created["id"], "binding": created["id"], "name": name,
            "data_center": profile.data_center, "size_gb": profile.volume_size_gb,
            "ownership": {"name": name, "created_by": "runonrunpod-lifecycle-v2"},
        }

    def ensure_cpu_endpoint(
        self, profile: ManagedProfile, session_id: str, volume: Mapping[str, object],
    ) -> Mapping[str, object]:
        if not profile.cpu_template_id:
            raise RunPodAdapterError("CPU profile requires cpu.template_id for RunPod endpoint creation")
        volume_id = volume.get("id")
        if not isinstance(volume_id, str) or not volume_id:
            raise RunPodAdapterError("CPU endpoint needs a recorded volume ID")
        cpu = self._cpu_configurations(profile)
        name = self._name(session_id, "cpu")
        existing = self._single_named(
            self._list_resources("/serverless", "endpoints", "CPU endpoint"), name, "CPU endpoint",
        )
        if existing is not None:
            raise RunPodAdapterError("matching CPU endpoint name is ambiguous; refuse unsafe reuse")
        payload: dict[str, object] = {
            "name": name, "templateId": profile.cpu_template_id, "type": "QUEUE", "cpu": cpu,
            "dataCenterIds": [profile.data_center], "networkVolumes": [volume_id],
            "workers": {"min": 0, "max": 1, "idleTimeout": profile.idle_timeout_seconds},
            # These preserve the v1 endpoint-create defaults that the former
            # adapter relied on implicitly.
            "scaling": {"type": "QUEUE_DELAY", "queueDelay": 4},
            "timeout": profile.execution_timeout_ms,
        }
        environment = dict(profile.cpu_environment)
        configured_binding = environment.get("STAGING_VOLUME_BINDING")
        if configured_binding is not None and configured_binding != volume_id:
            raise RunPodAdapterError("CPU environment binding conflicts with recorded volume ID")
        environment["STAGING_VOLUME_BINDING"] = volume_id
        payload["env"] = environment
        created = self._call("POST", "/serverless", payload)
        return self._verify_cpu_endpoint(created, name, profile, volume_id, cpu)

    def _verify_cpu_endpoint(
        self,
        value: object,
        name: str,
        profile: ManagedProfile,
        volume_id: str,
        requested_cpu: list[dict[str, object]],
    ) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not isinstance(value.get("id"), str):
            raise RunPodAdapterError("RunPod returned invalid CPU endpoint creation response")
        volumes = value.get("networkVolumes")
        cpu = value.get("cpu")
        workers = value.get("workers")
        scaling = value.get("scaling")
        attached = isinstance(volumes, list) and volume_id in volumes
        observed_cpu = {
            (item.get("id"), item.get("vcpuCount"))
            for item in cpu if isinstance(item, Mapping)
        } if isinstance(cpu, list) else set()
        expected_cpu = {(item["id"], item["vcpuCount"]) for item in requested_cpu}
        if (
            value.get("type") != "QUEUE"
            or value.get("name") != name
            or not attached
            or observed_cpu != expected_cpu
        ):
            raise RunPodAdapterError("RunPod CPU endpoint does not match requested compute/volume binding")
        if (
            not isinstance(workers, Mapping)
            or workers.get("min") != 0
            or workers.get("max") != 1
            or not isinstance(scaling, Mapping)
            or scaling.get("type") != "QUEUE_DELAY"
            or scaling.get("queueDelay") != 4
            or value.get("timeout") != profile.execution_timeout_ms
        ):
            raise RunPodAdapterError("RunPod CPU endpoint violates required worker limits")
        return {
            "id": value["id"], "name": name, "compute_type": "CPU", "volume_id": volume_id,
            "ownership": {"name": name, "created_by": "runonrunpod-lifecycle-v2"},
        }

    def get_resource(self, resource: str, resource_id: str) -> Mapping[str, object] | None:
        if resource == "volume":
            items = self._list_resources("/network-volumes", "networkVolumes", "volume")
            for item in items:
                if isinstance(item, Mapping) and item.get("id") == resource_id:
                    return {"id": resource_id, "binding": resource_id, **dict(item)}
            return None
        if resource in {"cpu_endpoint", "gpu_endpoint"}:
            try:
                value = self._call("GET", f"/serverless/{resource_id}")
            except RunPodAdapterError as error:
                if "HTTP 404" in str(error):
                    return None
                raise
            return dict(value) if isinstance(value, Mapping) else None
        raise RunPodAdapterError("unsupported RunPod resource type")

    def delete_resource(self, resource: str, resource_id: str) -> None:
        if resource == "volume":
            self._call("DELETE", f"/network-volumes/{resource_id}")
            return
        if resource in {"cpu_endpoint", "gpu_endpoint"}:
            self._call("DELETE", f"/serverless/{resource_id}")
            return
        raise RunPodAdapterError("unsupported RunPod resource type")
