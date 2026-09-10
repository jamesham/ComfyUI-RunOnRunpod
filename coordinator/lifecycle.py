"""Provider-neutral managed-session lifecycle with durable intent records.

The provider protocol is deliberately small and fakeable. Provider adapters
must return resources with enough immutable ownership evidence for a later
recovery or deletion to reject lookalike resources.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Callable, Mapping, Protocol

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
    cpu_template_id: str | None = None
    cpu_flavor_ids: tuple[str, ...] = ()
    cpu_vcpu_count: int | None = None
    idle_timeout_seconds: int = 5
    execution_timeout_ms: int = 3_600_000

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
        template_id = cpu.get("template_id")
        if template_id is not None and (not isinstance(template_id, str) or not template_id):
            raise LifecycleError("cpu.template_id must be a non-empty string")
        flavors = cpu.get("flavor_ids", [])
        if not isinstance(flavors, list) or not all(isinstance(item, str) and item for item in flavors):
            raise LifecycleError("cpu.flavor_ids must be a list of non-empty strings")
        vcpu_count = cpu.get("vcpu_count")
        if vcpu_count is not None and (isinstance(vcpu_count, bool) or not isinstance(vcpu_count, int) or vcpu_count < 1):
            raise LifecycleError("cpu.vcpu_count must be a positive integer")
        idle_timeout = cpu.get("idle_timeout_seconds", 5)
        execution_timeout = cpu.get("execution_timeout_ms", 3_600_000)
        if isinstance(idle_timeout, bool) or not isinstance(idle_timeout, int) or idle_timeout < 0:
            raise LifecycleError("cpu.idle_timeout_seconds must be non-negative")
        if isinstance(execution_timeout, bool) or not isinstance(execution_timeout, int) or execution_timeout < 1:
            raise LifecycleError("cpu.execution_timeout_ms must be positive")
        return cls(
            profile_id, data_center, volume["size_gb"], cpu["image"], gpu_image,
            template_id, tuple(flavors), vcpu_count, idle_timeout, execution_timeout,
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "profile_version": PROFILE_VERSION, "profile_id": self.profile_id,
            "data_center": self.data_center,
            "volume": {"size_gb": self.volume_size_gb},
            "cpu": {
                "image": self.cpu_image, "min_workers": 0, "max_workers": 1,
                "template_id": self.cpu_template_id, "flavor_ids": list(self.cpu_flavor_ids),
                "vcpu_count": self.cpu_vcpu_count, "idle_timeout_seconds": self.idle_timeout_seconds,
                "execution_timeout_ms": self.execution_timeout_ms,
            },
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

    def _recorded_resource(
        self,
        resource: str,
        recorded: Mapping[str, object],
    ) -> dict[str, object] | None:
        """Return an observed resource only if its recorded ownership still fits."""
        resource_id = recorded.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            raise LifecycleError(f"recorded {resource} has no resource ID")
        observed = self.provider.get_resource(resource, resource_id)
        if observed is None:
            return None
        result = _resource(observed, resource)
        if result["id"] != resource_id:
            raise LifecycleError(f"provider returned a different {resource} ID")

        ownership = recorded.get("ownership")
        if ownership is not None:
            if not isinstance(ownership, Mapping):
                raise LifecycleError(f"recorded {resource} ownership is invalid")
            expected_name = ownership.get("name")
            if not isinstance(expected_name, str) or not expected_name:
                raise LifecycleError(f"recorded {resource} ownership has no name")
            if result.get("name") != expected_name:
                raise LifecycleError(f"provider {resource} no longer matches recorded ownership")
            # Keep locally recorded ownership evidence: provider list/get
            # responses do not carry coordinator-private provenance markers.
            result["ownership"] = dict(ownership)

        if resource == "cpu_endpoint" and isinstance(recorded.get("volume_id"), str):
            expected_volume = recorded["volume_id"]
            volumes = result.get("networkVolumeIds")
            attached = result.get("networkVolumeId") == expected_volume or (
                isinstance(volumes, list) and expected_volume in volumes
            )
            if "networkVolumeId" in result or "networkVolumeIds" in result:
                if not attached:
                    raise LifecycleError("provider CPU endpoint no longer has the recorded volume")
            result["volume_id"] = expected_volume
        return result

    def _recover_resource(
        self,
        session_id: str,
        resource: str,
        ensure: Callable[[], Mapping[str, object]],
    ) -> tuple[dict[str, object], dict[str, object]]:
        """Use a durable resource record when possible; otherwise create it.

        This distinction is important for providers where a repeated create
        after an uncertain response cannot be safely attributed by name alone.
        """
        session = self.coordinator.get_session(session_id)
        resources = session.get("resources")
        recorded = resources.get(resource) if isinstance(resources, Mapping) else None
        action = "reconcile" if isinstance(recorded, Mapping) else "create"
        operation = self._operation_id(session_id, action, resource)
        self.coordinator.record_lifecycle_operation(
            session_id, operation, action=action, resource=resource, state="intent",
        )
        try:
            if isinstance(recorded, Mapping):
                value = self._recorded_resource(resource, recorded)
                if value is None:
                    raise LifecycleError(f"recorded {resource} is missing; manual reconciliation required")
            else:
                value = _resource(ensure(), resource)
        except Exception as error:
            self.coordinator.record_lifecycle_operation(
                session_id, operation, action=action, resource=resource, state="failed",
                result={"error": str(error)}, session_state="recoverable",
            )
            phase = "provisioning" if action == "create" else "reconciliation"
            raise LifecycleError(f"{resource} {phase} failed: {error}") from None
        session = self.coordinator.record_lifecycle_operation(
            session_id, operation, action=action, resource=resource, state="succeeded", result=value,
        )
        return value, session

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
        existing_resources = session.get("resources")
        cpu_action = "reconcile" if (
            isinstance(existing_resources, Mapping)
            and isinstance(existing_resources.get("cpu_endpoint"), Mapping)
        ) else "create"

        volume, _ = self._recover_resource(
            session_id, "volume", lambda: self.provider.ensure_volume(profile, session_id),
        )
        cpu, _ = self._recover_resource(
            session_id, "cpu_endpoint",
            lambda: self.provider.ensure_cpu_endpoint(profile, session_id, volume),
        )
        return self.coordinator.record_lifecycle_operation(
            session_id, self._operation_id(session_id, cpu_action, "cpu_endpoint"),
            action=cpu_action, resource="cpu_endpoint", state="succeeded", result=cpu, session_state="preparing",
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
                observed = self._recorded_resource(resource, binding)
                if observed is None:
                    session = self.coordinator.record_lifecycle_operation(
                        session_id, operation, action="delete", resource=resource,
                        state="confirmed_absent", session_state="closing",
                    )
                    resources = session.get("resources") if isinstance(session.get("resources"), Mapping) else {}
                    continue
                self.provider.delete_resource(resource, observed["id"])
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
