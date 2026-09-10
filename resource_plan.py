"""Pure workflow-to-model resource planning for local and CPU staging paths."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Mapping, Sequence


RESOURCE_PLAN_VERSION = 1


class ResourcePlanError(ValueError):
    """A supported workflow model binding cannot form a safe resource plan."""


@dataclass(frozen=True)
class ModelBinding:
    node_id: str
    class_type: str
    input_name: str


@dataclass(frozen=True)
class ModelRequirement:
    subdir: str
    filename: str
    target_path: str
    bindings: tuple[ModelBinding, ...]


@dataclass(frozen=True)
class ModelResourcePlan:
    version: int
    requirements: tuple[ModelRequirement, ...]


@dataclass(frozen=True)
class ModelIdentity:
    """Immutable byte identity required before a model can be made ready."""

    sha256: str
    size: int


@dataclass(frozen=True)
class MaterializedModel:
    """A transfer-ready requirement with an exact expected byte identity."""

    requirement: ModelRequirement
    identity: ModelIdentity
    local_path: str | None
    descriptor: dict[str, object] | None


def _fields(value: object) -> Sequence[tuple[str, str]]:
    if isinstance(value, tuple) and len(value) == 2:
        return (value,)
    if isinstance(value, list) and all(
        isinstance(item, tuple) and len(item) == 2 for item in value
    ):
        return tuple(value)
    raise ResourcePlanError("model field registry contains an invalid entry")


def _component(value: object, location: str) -> str:
    if not isinstance(value, str) or not value or any(
        part in ("", ".", "..") for part in value.split("/")
    ):
        raise ResourcePlanError(f"{location} is not a portable relative path")
    if value.startswith("/") or "\\" in value or ":" in value or "\0" in value:
        raise ResourcePlanError(f"{location} is not a portable relative path")
    return value


def compile_model_resource_plan(
    workflow: object,
    model_node_fields: Mapping[str, object],
) -> ModelResourcePlan:
    """Return deterministic, safe requirements for explicitly supported nodes.

    Unknown nodes remain outside the plan. Known model inputs that are absent or
    linked rather than filename-valued are left to ComfyUI's existing validation,
    preserving the plugin's current behavior. A string filename must be portable
    because it becomes both an S3 key component and a local staging target.
    """
    if not isinstance(workflow, Mapping):
        raise ResourcePlanError("workflow must be an object")

    grouped: dict[str, tuple[str, str, list[ModelBinding]]] = {}
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not node_id:
            raise ResourcePlanError("workflow node ID must be a non-empty string")
        if not isinstance(node, Mapping):
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str):
            continue
        fields = model_node_fields.get(class_type)
        if fields is None:
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            continue
        for input_name, subdir_value in _fields(fields):
            if not isinstance(input_name, str):
                raise ResourcePlanError("model field registry input name must be a string")
            filename_value = inputs.get(input_name)
            if filename_value is None or not isinstance(filename_value, str):
                continue
            subdir = _component(subdir_value, f"{class_type}.{input_name} subdirectory")
            filename = _component(filename_value, f"node {node_id} {input_name}")
            target_path = f"models/{subdir}/{filename}"
            key = target_path.casefold()
            binding = ModelBinding(node_id, class_type, input_name)
            current = grouped.get(key)
            if current is None:
                grouped[key] = (subdir, filename, [binding])
            elif current[:2] != (subdir, filename):
                raise ResourcePlanError(
                    f"model target path has a case-insensitive collision: {target_path}"
                )
            else:
                current[2].append(binding)

    requirements = tuple(
        ModelRequirement(
            subdir=subdir,
            filename=filename,
            target_path=f"models/{subdir}/{filename}",
            bindings=tuple(bindings),
        )
        for _key, (subdir, filename, bindings) in sorted(grouped.items())
    )
    return ModelResourcePlan(RESOURCE_PLAN_VERSION, requirements)


def model_resource_plan_dict(plan: ModelResourcePlan) -> dict[str, object]:
    """Serialize a plan without local paths, credentials, or source URLs."""
    if not isinstance(plan, ModelResourcePlan) or plan.version != RESOURCE_PLAN_VERSION:
        raise ResourcePlanError("unsupported model resource plan")
    return {
        "resource_plan_version": plan.version,
        "models": [
            {
                "target_path": requirement.target_path,
                "bindings": [
                    {
                        "node_id": binding.node_id,
                        "class_type": binding.class_type,
                        "input_name": binding.input_name,
                    }
                    for binding in requirement.bindings
                ],
            }
            for requirement in plan.requirements
        ],
    }


def model_resource_plan_sha256(plan: ModelResourcePlan) -> str:
    """Stable identity for a workflow's supported model bindings."""
    encoded = json.dumps(
        model_resource_plan_dict(plan), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def model_identity(sha256: object, size: object) -> ModelIdentity:
    """Validate and normalize an externally supplied immutable identity."""
    if not isinstance(sha256, str) or len(sha256) != 64:
        raise ResourcePlanError("model SHA-256 must be a 64-character hexadecimal string")
    try:
        int(sha256, 16)
    except ValueError:
        raise ResourcePlanError("model SHA-256 must be a 64-character hexadecimal string") from None
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ResourcePlanError("model size must be a non-negative integer")
    return ModelIdentity(sha256.lower(), size)


def local_model_identity(path: str) -> ModelIdentity:
    """Hash a local file once to turn it into an immutable upload source."""
    try:
        size = os.path.getsize(path)
        h = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError as error:
        raise ResourcePlanError(f"could not identify local model {path!r}: {error}") from None
    return ModelIdentity(h.hexdigest(), size)


def materialize_model_requirement(
    requirement: ModelRequirement,
    local_path: str | None,
    descriptor: Mapping[str, object] | None,
) -> MaterializedModel | None:
    """Choose a local upload or remote source with an exact byte identity.

    A local file always supplies a complete identity. A remote source is usable
    only when it declares the same strict identity (when a local fallback is
    present) or declares one itself. Filename-only and mutable-source lookup
    results are intentionally not materialized.
    """
    local_identity = local_model_identity(local_path) if local_path else None
    if descriptor is not None:
        if descriptor.get("dest_path") != requirement.target_path:
            raise ResourcePlanError("source target does not match the model requirement")
        url = descriptor.get("url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ResourcePlanError("model source URL must use HTTPS")
        try:
            remote_identity = model_identity(
                descriptor.get("expected_sha256"),
                descriptor.get("expected_size"),
            )
        except ResourcePlanError:
            remote_identity = None
        if remote_identity is not None:
            if local_identity is None or local_identity == remote_identity:
                return MaterializedModel(
                    requirement, remote_identity, local_path, dict(descriptor),
                )

    if local_identity is not None:
        return MaterializedModel(requirement, local_identity, local_path, None)
    return None
