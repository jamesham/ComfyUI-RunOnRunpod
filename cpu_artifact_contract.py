"""Signed, bounded CPU-volume artifact operations.

CPU-managed sessions use this contract for files that cannot be fetched from a
model provider: local-model fallback, ComfyUI inputs, output retrieval, and
per-job cleanup.  It intentionally travels only through RunPod's authenticated
Serverless HTTPS API; it does not use a network volume's S3 interface.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Mapping


CPU_ARTIFACT_PROTOCOL_VERSION = 1
MAX_CHUNK_BYTES = 4 * 1024 * 1024
_ACTIONS = frozenset({"write", "read", "delete"})
_ROOTS = frozenset({"models", "inputs", "outputs"})


class CpuArtifactContractError(ValueError):
    """An artifact request or response is malformed or unsafe."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 128:
        raise CpuArtifactContractError(f"artifact request requires a valid {label}")
    if any(not (character.isascii() and (character.isalnum() or character in "_-")) for character in value):
        raise CpuArtifactContractError(f"artifact request requires a valid {label}")
    return value


def _target_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        raise CpuArtifactContractError("artifact target is not a safe relative path")
    parts = value.split("/")
    if len(parts) < 2 or parts[0] not in _ROOTS or any(part in ("", ".", "..") for part in parts):
        raise CpuArtifactContractError("artifact target is not a safe relative path")
    return value


def _sha256(value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CpuArtifactContractError("artifact request requires a SHA-256")
    try:
        int(value, 16)
    except ValueError:
        raise CpuArtifactContractError("artifact request requires a SHA-256") from None
    return value.lower()


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CpuArtifactContractError(f"artifact request requires a non-negative {label}")
    return value


def _chunk(value: object) -> tuple[str, bytes]:
    if not isinstance(value, str):
        raise CpuArtifactContractError("artifact write data must be base64 text")
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise CpuArtifactContractError("artifact write data is not valid base64") from None
    if len(decoded) > MAX_CHUNK_BYTES:
        raise CpuArtifactContractError("artifact write chunk is too large")
    return value, decoded


def validate_artifact_request(value: object) -> dict[str, object]:
    """Validate before the CPU worker performs filesystem activity."""
    if not isinstance(value, Mapping):
        raise CpuArtifactContractError("artifact request must be an object")
    if value.get("protocol_version") != CPU_ARTIFACT_PROTOCOL_VERSION:
        raise CpuArtifactContractError("unsupported CPU artifact protocol version")
    action = value.get("action")
    if action not in _ACTIONS:
        raise CpuArtifactContractError("artifact action is not supported")
    normalized: dict[str, object] = {
        "protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
        "operation_id": _identifier(value.get("operation_id"), "operation ID"),
        "volume_binding": _identifier(value.get("volume_binding"), "volume binding"),
        "action": action,
        "target_path": _target_path(value.get("target_path")),
    }
    if action == "write":
        normalized["transfer_id"] = _identifier(value.get("transfer_id"), "transfer ID")
        normalized["offset"] = _nonnegative_integer(value.get("offset"), "offset")
        encoded, _decoded = _chunk(value.get("data"))
        normalized["data"] = encoded
        if not isinstance(value.get("complete"), bool):
            raise CpuArtifactContractError("artifact write requires a complete flag")
        normalized["complete"] = value["complete"]
        normalized["expected_sha256"] = _sha256(value.get("expected_sha256"))
        normalized["expected_size"] = _nonnegative_integer(value.get("expected_size"), "size")
    elif action == "read":
        normalized["offset"] = _nonnegative_integer(value.get("offset"), "offset")
        length = _nonnegative_integer(value.get("length"), "length")
        if length < 1 or length > MAX_CHUNK_BYTES:
            raise CpuArtifactContractError("artifact read length is outside the permitted range")
        normalized["length"] = length
    return normalized


def sign_artifact_request(request: object, key: str) -> dict[str, object]:
    payload = validate_artifact_request(request)
    if not isinstance(key, str) or not key:
        raise CpuArtifactContractError("artifact signing key is required")
    signature = hmac.new(key.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()
    return {"payload": payload, "signature": signature}


def verify_signed_artifact_request(envelope: object, key: str) -> dict[str, object]:
    if not isinstance(envelope, Mapping):
        raise CpuArtifactContractError("signed artifact request must be an object")
    signature = envelope.get("signature")
    if not isinstance(signature, str) or not isinstance(key, str) or not key:
        raise CpuArtifactContractError("signed artifact request is missing a signature")
    payload = validate_artifact_request(envelope.get("payload"))
    expected = hmac.new(key.encode("utf-8"), _canonical_bytes(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise CpuArtifactContractError("signed artifact request signature is invalid")
    return payload


def unsigned_artifact_payload(envelope: object) -> dict[str, object]:
    if not isinstance(envelope, Mapping):
        raise CpuArtifactContractError("signed artifact request must be an object")
    return validate_artifact_request(envelope.get("payload"))
