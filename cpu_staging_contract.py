"""Versioned, signed contract shared by the plugin and CPU staging image.

The browser never creates these envelopes. A future session coordinator signs
the request with a key trusted by the CPU deployment; this module only defines
the portable payload and deterministic validation rules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Mapping

try:  # Package import in the ComfyUI plugin; top-level import in worker-cpu.
    from .resource_plan import ResourcePlanError, model_identity
except ImportError:  # pragma: no cover - exercised in the container image.
    from resource_plan import ResourcePlanError, model_identity


CPU_STAGING_PROTOCOL_VERSION = 1
_VALID_AUTH = {"none", "hf", "civitai"}


class CpuStagingContractError(ValueError):
    """A CPU staging request, envelope, or result is unsafe or malformed."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _relative_model_target(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("models/"):
        raise CpuStagingContractError("model target must be under models/")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts) or "\\" in value or "\0" in value:
        raise CpuStagingContractError("model target is not a safe relative path")
    return value


def validate_stage_request(value: object) -> dict[str, object]:
    """Normalize a request before any provider or filesystem activity."""
    if not isinstance(value, Mapping):
        raise CpuStagingContractError("stage request must be an object")
    if value.get("protocol_version") != CPU_STAGING_PROTOCOL_VERSION:
        raise CpuStagingContractError("unsupported CPU staging protocol version")
    operation_id = value.get("operation_id")
    volume_binding = value.get("volume_binding")
    models = value.get("models")
    if not isinstance(operation_id, str) or not operation_id:
        raise CpuStagingContractError("stage request requires an operation_id")
    if not isinstance(volume_binding, str) or not volume_binding:
        raise CpuStagingContractError("stage request requires a volume_binding")
    if not isinstance(models, list):
        raise CpuStagingContractError("stage request models must be a list")

    normalized: list[dict[str, object]] = []
    targets: set[str] = set()
    for model in models:
        if not isinstance(model, Mapping):
            raise CpuStagingContractError("stage model must be an object")
        target = _relative_model_target(model.get("target_path"))
        if target.casefold() in targets:
            raise CpuStagingContractError("stage request has duplicate model targets")
        targets.add(target.casefold())
        url = model.get("url")
        auth = model.get("auth")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise CpuStagingContractError("stage model URL must use HTTPS")
        if auth not in _VALID_AUTH:
            raise CpuStagingContractError("stage model auth is not supported")
        try:
            identity = model_identity(model.get("expected_sha256"), model.get("expected_size"))
        except ResourcePlanError as error:
            raise CpuStagingContractError(str(error)) from None
        normalized.append({
            "target_path": target,
            "url": url,
            "expected_sha256": identity.sha256,
            "expected_size": identity.size,
            "auth": auth,
        })
    return {
        "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
        "operation_id": operation_id,
        "volume_binding": volume_binding,
        "models": normalized,
    }


def stage_request_from_downloads(
    operation_id: str,
    volume_binding: str,
    downloads: list[dict],
) -> dict[str, object]:
    """Produce the unsigned payload that a coordinator must sign."""
    models = []
    for download in downloads:
        model = dict(download)
        # The legacy GPU protocol calls the same relative path ``dest_path``.
        # Normalize it at the boundary rather than leaking that protocol name
        # into the independent CPU staging contract.
        model["target_path"] = model.pop("dest_path", model.get("target_path"))
        models.append(model)
    return validate_stage_request({
        "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
        "operation_id": operation_id,
        "volume_binding": volume_binding,
        "models": models,
    })


def sign_stage_request(request: object, key: str) -> dict[str, object]:
    """Coordinator-only helper; do not expose its key to browser settings."""
    payload = validate_stage_request(request)
    if not isinstance(key, str) or not key:
        raise CpuStagingContractError("signing key is required")
    signature = hmac.new(key.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": signature}


def verify_signed_stage_request(envelope: object, key: str) -> dict[str, object]:
    """Verify an envelope received by the CPU deployment."""
    if not isinstance(envelope, Mapping):
        raise CpuStagingContractError("signed stage request must be an object")
    signature = envelope.get("signature")
    if not isinstance(signature, str) or not isinstance(key, str) or not key:
        raise CpuStagingContractError("signed stage request is missing a signature")
    payload = validate_stage_request(envelope.get("payload"))
    expected = hmac.new(key.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise CpuStagingContractError("signed stage request signature is invalid")
    return payload


def unsigned_stage_payload(envelope: object) -> dict[str, object]:
    """Validate payload shape locally without attempting signature verification."""
    if not isinstance(envelope, Mapping):
        raise CpuStagingContractError("signed stage request must be an object")
    return validate_stage_request(envelope.get("payload"))


def validate_stage_result(value: object, request: object) -> dict[str, object]:
    """Require one successful, identity-preserving result for every target."""
    expected = validate_stage_request(request)
    if not isinstance(value, Mapping):
        raise CpuStagingContractError("CPU staging result must be an object")
    if value.get("protocol_version") != CPU_STAGING_PROTOCOL_VERSION:
        raise CpuStagingContractError("CPU staging result protocol mismatch")
    if value.get("operation_id") != expected["operation_id"]:
        raise CpuStagingContractError("CPU staging result operation mismatch")
    if value.get("status") != "success" or not isinstance(value.get("results"), list):
        raise CpuStagingContractError("CPU staging did not report success")
    actual = {item.get("target_path"): item for item in value["results"] if isinstance(item, Mapping)}
    normalized_results = []
    for model in expected["models"]:
        result = actual.get(model["target_path"])
        if not isinstance(result, Mapping) or result.get("status") != "done":
            raise CpuStagingContractError(f"CPU staging did not complete {model['target_path']}")
        try:
            identity = model_identity(result.get("sha256"), result.get("size"))
        except ResourcePlanError as error:
            raise CpuStagingContractError(str(error)) from None
        if identity.sha256 != model["expected_sha256"] or identity.size != model["expected_size"]:
            raise CpuStagingContractError(f"CPU staging identity mismatch for {model['target_path']}")
        normalized_results.append({
            "target_path": model["target_path"], "status": "done",
            "sha256": identity.sha256, "size": identity.size,
        })
    return {
        "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
        "operation_id": expected["operation_id"],
        "status": "success",
        "results": normalized_results,
    }
