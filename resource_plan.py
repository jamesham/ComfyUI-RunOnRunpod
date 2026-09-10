"""Pure workflow-to-model resource planning for local and CPU staging paths."""

from __future__ import annotations

import hashlib
import json
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
