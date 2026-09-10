"""Local, crash-safe records for recipes and managed creative sessions.

This module intentionally has no RunPod API client. It records intent and
produces signed CPU-stage envelopes; a later lifecycle adapter performs remote
create/delete calls between those durable transitions.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid
from typing import Mapping

try:  # Plugin package import; see fallback for direct coordinator tooling.
    from ..cpu_staging_contract import (
        CpuStagingContractError,
        sign_stage_request,
        stage_request_from_downloads,
        validate_stage_result,
    )
    from ..resource_plan import ModelResourcePlan, model_resource_plan_dict, model_resource_plan_sha256
except ImportError:  # pragma: no cover - exercised by direct CLI/library use.
    from cpu_staging_contract import (
        CpuStagingContractError,
        sign_stage_request,
        stage_request_from_downloads,
        validate_stage_result,
    )
    from resource_plan import ModelResourcePlan, model_resource_plan_dict, model_resource_plan_sha256


RECIPE_VERSION = 1
SESSION_VERSION = 1
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


class CoordinatorError(RuntimeError):
    pass


def _now() -> int:
    return int(time.time())


def _id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise CoordinatorError(f"{label} must contain only letters, digits, _ or -")
    return value


def _relative_reference(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise CoordinatorError("local recipe reference must be a non-empty relative path")
    if value.startswith("/") or "\\" in value or "\0" in value:
        raise CoordinatorError("local recipe reference must be a portable relative path")
    if any(part in ("", ".", "..") for part in value.split("/")):
        raise CoordinatorError("local recipe reference must be a portable relative path")
    return value


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CoordinatorError(f"record does not exist: {path.name}") from None
    except (OSError, json.JSONDecodeError) as error:
        raise CoordinatorError(f"record is unreadable: {path.name}: {error}") from None
    if not isinstance(value, dict):
        raise CoordinatorError(f"record is not an object: {path.name}")
    return value


class SessionCoordinator:
    """Owns local recipe/session records and coordinator-only signing.

    ``root`` must be an explicitly selected local state directory. The signing
    key is supplied for each request and is never persisted in these records.
    """

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _recipe_path(self, recipe_id: str) -> Path:
        return self.root / "recipes" / recipe_id / "recipe.json"

    def _session_path(self, session_id: str) -> Path:
        return self.root / "sessions" / session_id / "session.json"

    def _lock_path(self, record: Path) -> Path:
        return record.with_suffix(record.suffix + ".lock")

    def _with_lock(self, record: Path):
        record.parent.mkdir(parents=True, exist_ok=True)
        return open(self._lock_path(record), "a+b")

    def save_recipe(
        self,
        recipe_id: str,
        plan: ModelResourcePlan,
        materialized: Mapping[str, object],
        *,
        local_references: Mapping[str, str] | None = None,
        workflow_sha256: str | None = None,
    ) -> dict[str, object]:
        """Persist a reconstructible working-set recipe without credentials.

        ``materialized`` is keyed by target path and supplies ``identity``,
        ``descriptor``, and ``local_path`` from the preparation layer. Local
        files require an explicit portable reference so a deleted volume can be
        recreated without saving an absolute machine path.
        """
        recipe_id = _id(recipe_id, "recipe ID")
        local_references = local_references or {}
        models = []
        for requirement in plan.requirements:
            resolved = materialized.get(requirement.target_path)
            if resolved is None:
                raise CoordinatorError(f"recipe is missing materialization for {requirement.target_path}")
            identity = getattr(resolved, "identity", None)
            descriptor = getattr(resolved, "descriptor", None)
            if not identity or not isinstance(getattr(identity, "sha256", None), str):
                raise CoordinatorError(f"recipe has no immutable identity for {requirement.target_path}")
            source: dict[str, object]
            if isinstance(descriptor, Mapping):
                source = {
                    "kind": "remote", "url": descriptor.get("url"), "auth": descriptor.get("auth"),
                }
            else:
                source = {"kind": "local", "reference": _relative_reference(local_references.get(requirement.target_path))}
            models.append({
                "target_path": requirement.target_path,
                "sha256": identity.sha256,
                "size": identity.size,
                "bindings": [
                    {"node_id": binding.node_id, "class_type": binding.class_type, "input_name": binding.input_name}
                    for binding in requirement.bindings
                ],
                "source": source,
            })

        path = self._recipe_path(recipe_id)
        with self._with_lock(path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                revision = 1
                if path.exists():
                    revision = int(_read_json(path).get("revision", 0)) + 1
                record = {
                    "recipe_version": RECIPE_VERSION, "recipe_id": recipe_id, "revision": revision,
                    "updated_at": _now(), "workflow_sha256": workflow_sha256,
                    "resource_plan_sha256": model_resource_plan_sha256(plan),
                    "resource_plan": model_resource_plan_dict(plan), "models": models,
                }
                _atomic_json(path, record)
                return record
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def create_session(
        self,
        recipe_id: str,
        *,
        volume_binding: str,
        cpu_endpoint_id: str,
        gpu_endpoint_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, object]:
        """Record a planned session before any provider resources are created."""
        recipe_id = _id(recipe_id, "recipe ID")
        if not self._recipe_path(recipe_id).exists():
            raise CoordinatorError(f"recipe does not exist: {recipe_id}")
        session_id = _id(session_id or uuid.uuid4().hex, "session ID")
        if not isinstance(volume_binding, str) or not volume_binding:
            raise CoordinatorError("session requires a volume binding")
        if not isinstance(cpu_endpoint_id, str) or not cpu_endpoint_id:
            raise CoordinatorError("session requires a CPU endpoint ID")
        if gpu_endpoint_id is not None and not isinstance(gpu_endpoint_id, str):
            raise CoordinatorError("GPU endpoint ID must be a string")
        path = self._session_path(session_id)
        with self._with_lock(path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                if path.exists():
                    raise CoordinatorError(f"session already exists: {session_id}")
                recipe = _read_json(self._recipe_path(recipe_id))
                record = {
                    "session_version": SESSION_VERSION, "session_id": session_id,
                    "recipe_id": recipe_id, "recipe_revision": recipe["revision"],
                    "resource_plan_sha256": recipe["resource_plan_sha256"],
                    "state": "planned", "created_at": _now(), "updated_at": _now(),
                    "bindings": {
                        "volume_binding": volume_binding, "cpu_endpoint_id": cpu_endpoint_id,
                        "gpu_endpoint_id": gpu_endpoint_id,
                    },
                    "preparations": {},
                }
                _atomic_json(path, record)
                return record
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def authorize_stage(
        self,
        session_id: str,
        prep_id: str,
        downloads: list[dict],
        signing_key: str,
    ) -> dict[str, object]:
        """Persist stage intent, then return the coordinator-signed envelope."""
        session_id = _id(session_id, "session ID")
        prep_id = _id(prep_id, "preparation ID")
        path = self._session_path(session_id)
        with self._with_lock(path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                record = _read_json(path)
                if record.get("state") not in ("planned", "preparing", "ready"):
                    raise CoordinatorError(f"session cannot stage while {record.get('state')}")
                bindings = record.get("bindings")
                if not isinstance(bindings, Mapping):
                    raise CoordinatorError("session bindings are invalid")
                try:
                    request = stage_request_from_downloads(
                        prep_id, bindings.get("volume_binding", ""), downloads,
                    )
                except CpuStagingContractError as error:
                    raise CoordinatorError(f"cannot authorize stage request: {error}") from None
                preparations = record.setdefault("preparations", {})
                if not isinstance(preparations, dict):
                    raise CoordinatorError("session preparations are invalid")
                existing = preparations.get(prep_id)
                request_hash = _canonical_hash(request)
                if isinstance(existing, Mapping) and existing.get("request_sha256") != request_hash:
                    raise CoordinatorError("preparation ID was already authorized for different content")
                preparations[prep_id] = {
                    "state": "authorized", "authorized_at": _now(),
                    "request_sha256": request_hash, "request": request,
                }
                record["state"] = "preparing"
                record["updated_at"] = _now()
                _atomic_json(path, record)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return sign_stage_request(request, signing_key)

    def record_stage_result(self, session_id: str, prep_id: str, result: object) -> dict[str, object]:
        """Accept only a complete validated CPU result and make the session ready."""
        session_id = _id(session_id, "session ID")
        prep_id = _id(prep_id, "preparation ID")
        path = self._session_path(session_id)
        with self._with_lock(path) as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                record = _read_json(path)
                preparation = (record.get("preparations") or {}).get(prep_id)
                if not isinstance(preparation, Mapping) or not isinstance(preparation.get("request"), Mapping):
                    raise CoordinatorError("unknown preparation")
                try:
                    verified = validate_stage_result(result, preparation["request"])
                except CpuStagingContractError as error:
                    raise CoordinatorError(f"CPU stage result is not ready: {error}") from None
                updated = dict(preparation)
                updated.update({"state": "ready", "completed_at": _now(), "result": verified})
                record["preparations"][prep_id] = updated
                record["state"] = "ready"
                record["updated_at"] = _now()
                _atomic_json(path, record)
                return record
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def get_session(self, session_id: str) -> dict[str, object]:
        return _read_json(self._session_path(_id(session_id, "session ID")))
