"""Provider-neutral managed-session lifecycle with durable intent records.

The provider protocol is deliberately small and fakeable. A RunPod-specific
adapter can implement it later without changing coordinator persistence rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping, Protocol

from .session_store import CoordinatorError, SessionCoordinator


PROFILE_VERSION = 1


class LifecycleError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagedProfile:
    profile_id: str
    data_center: str
    volume_size_gb: int
    cpu_image: str
    gpu_image: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "ManagedProfile":
        if not isinstance(value, Mapping) or value.get("profile_version") != PROFILE_VERSION:
            raise LifecycleError("unsupported managed profile version")
        profile_id = value.get("profile_id")
        data_center = value.get("data_center")
        volume = value.get("volume")
        cpu = value.get("cpu")
        gpu = value.get("gpu")
        if not isinstance(profile_id, str) or not profile_id:
            raise LifecycleError("managed profile requires profile_id")
        if not isinstance(data_center, str) or not data_center:
            raise LifecycleError("managed profile requires data_center")
        if not isinstance(volume, Mapping) or isinstance(volume.get("size_gb"), bool) or not isinstance(volume.get("size_gb"), int) or volume["size_gb"] <= 0:
            raise LifecycleError("managed profile requires positive volume.size_gb")
        if not isinstance(cpu, Mapping) or not isinstance(cpu.get("image"), str) or not cpu["image"]:
            raise LifecycleError("managed profile requires cpu.image")
        if cpu.get("min_workers", 0) != 0 or cpu.get("max_workers", 1) != 1:
            raise LifecycleError("CPU profile must use min_workers=0 and max_workers=1")
        gpu_image = None
        if gpu is not None:
            if not isinstance(gpu, Mapping) or not isinstance(gpu.get("image"), str) or not gpu["image"]:
                raise LifecycleError("managed GPU profile requires gpu.image")
            gpu_image = gpu["image"]
        return cls(profile_id, data_center, volume["size_gb"], cpu["image"], gpu_image)

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "profile_version": PROFILE_VERSION, "profile_id": self.profile_id,
            "data_center": self.data_center,
            "volume": {"size_gb": self.volume_size_gb},
            "cpu": {"image": self.cpu_image, "min_workers": 0, "max_workers": 1},
        }
        if self.gpu_image:
            value["gpu"] = {"image": self.gpu_image, "min_workers": 0, "max_workers": 1}
        return value


class LifecycleProvider(Protocol):
    """The only external authority required by the lifecycle service."""

    def ensure_volume(self, profile: ManagedProfile, session_id: str) -> Mapping[str, object]: ...
    def ensure_cpu_endpoint(self, profile: ManagedProfile, session_id: str, volume: Mapping[str, object]) -> Mapping[str, object]: ...
    def get_resource(self, resource: str, resource_id: str) -> Mapping[str, object] | None: ...
    def delete_resource(self, resource: str, resource_id: str) -> None: ...


def _resource(value: Mapping[str, object], kind: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or not isinstance(value.get("id"), str) or not value["id"]:
        raise LifecycleError(f"provider returned invalid {kind} resource")
    result = dict(value)
    if kind == "volume" and (not isinstance(result.get("binding"), str) or not result["binding"]):
        raise LifecycleError("provider volume resource requires immutable binding")
    return result


class SessionLifecycleService:
    """Creates, reconciles, and closes resources around durable coordinator state."""

    def __init__(self, coordinator: SessionCoordinator, provider: LifecycleProvider) -> None:
        self.coordinator = coordinator
        self.provider = provider

    @staticmethod
    def _operation_id(session_id: str, action: str, resource: str) -> str:
        digest = hashlib.sha256(f"{session_id}:{action}:{resource}".encode()).hexdigest()[:16]
        return f"{action}-{resource}-{digest}"

    def start(self, recipe_id: str, profile: ManagedProfile, *, session_id: str | None = None) -> dict[str, object]:
        """Persist intent, then ensure a volume and one CPU endpoint exist.

        The provider must make ``ensure_*`` idempotent/reconciling by session
        ownership. This service intentionally does not create a GPU endpoint:
        GPU provisioning follows verified CPU staging in a later increment.
        """
        self.coordinator.save_profile(profile.profile_id, profile.to_dict())
        session = self.coordinator.create_provisioning_session(
            recipe_id, profile.profile_id, session_id=session_id,
        )
        return self.recover(session["session_id"], profile)

    def recover(self, session_id: str, profile: ManagedProfile) -> dict[str, object]:
        """Re-run idempotent provider reconciliation after a crash or restart."""
        session = self.coordinator.get_session(session_id)
        if session.get("state") in {"closed", "closing"}:
            raise LifecycleError(f"cannot recover session while {session.get('state')}")

        volume_op = self._operation_id(session_id, "create", "volume")
        self.coordinator.record_lifecycle_operation(
            session_id, volume_op, action="create", resource="volume", state="intent",
        )
        try:
            volume = _resource(self.provider.ensure_volume(profile, session_id), "volume")
        except Exception as error:
            self.coordinator.record_lifecycle_operation(
                session_id, volume_op, action="create", resource="volume", state="failed",
                result={"error": str(error)}, session_state="recoverable",
            )
            raise LifecycleError(f"volume provisioning failed: {error}") from None
        self.coordinator.record_lifecycle_operation(
            session_id, volume_op, action="create", resource="volume", state="succeeded", result=volume,
        )

        cpu_op = self._operation_id(session_id, "create", "cpu_endpoint")
        self.coordinator.record_lifecycle_operation(
            session_id, cpu_op, action="create", resource="cpu_endpoint", state="intent",
        )
        try:
            cpu = _resource(self.provider.ensure_cpu_endpoint(profile, session_id, volume), "cpu_endpoint")
        except Exception as error:
            self.coordinator.record_lifecycle_operation(
                session_id, cpu_op, action="create", resource="cpu_endpoint", state="failed",
                result={"error": str(error)}, session_state="recoverable",
            )
            raise LifecycleError(f"CPU endpoint provisioning failed: {error}") from None
        return self.coordinator.record_lifecycle_operation(
            session_id, cpu_op, action="create", resource="cpu_endpoint", state="succeeded",
            result=cpu, session_state="preparing",
        )

    def end(self, session_id: str) -> dict[str, object]:
        """Persist closure intent and delete exact recorded resources in order."""
        session = self.coordinator.get_session(session_id)
        if session.get("state") == "closed":
            return session
        resources = session.get("resources")
        if not isinstance(resources, Mapping):
            raise LifecycleError("session resources are invalid")
        self.coordinator.record_lifecycle_operation(
            session_id, self._operation_id(session_id, "delete", "volume"),
            action="delete", resource="volume", state="intent", session_state="closing",
        )
        for resource in ("gpu_endpoint", "cpu_endpoint", "volume"):
            binding = resources.get(resource)
            if not isinstance(binding, Mapping) or not isinstance(binding.get("id"), str):
                continue
            operation = self._operation_id(session_id, "delete", resource)
            self.coordinator.record_lifecycle_operation(
                session_id, operation, action="delete", resource=resource, state="intent", session_state="closing",
            )
            try:
                self.provider.delete_resource(resource, binding["id"])
                remaining = self.provider.get_resource(resource, binding["id"])
            except Exception as error:
                self.coordinator.record_lifecycle_operation(
                    session_id, operation, action="delete", resource=resource, state="failed",
                    result={"error": str(error)}, session_state="recoverable",
                )
                raise LifecycleError(f"could not delete {resource}: {error}") from None
            if remaining is not None:
                self.coordinator.record_lifecycle_operation(
                    session_id, operation, action="delete", resource=resource, state="failed",
                    result={"error": "provider still reports resource"}, session_state="recoverable",
                )
                raise LifecycleError(f"provider still reports {resource} after deletion")
            session = self.coordinator.record_lifecycle_operation(
                session_id, operation, action="delete", resource=resource, state="confirmed_absent", session_state="closing",
            )
            resources = session.get("resources") if isinstance(session.get("resources"), Mapping) else {}
        return self.coordinator.record_lifecycle_operation(
            session_id, self._operation_id(session_id, "delete", "volume"),
            action="delete", resource="volume", state="confirmed_absent", session_state="closed",
        )
