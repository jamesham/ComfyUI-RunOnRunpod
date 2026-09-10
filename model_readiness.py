"""Portable readiness receipts for verified model objects on a volume."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Mapping

from .resource_plan import ModelIdentity, ModelRequirement, ResourcePlanError, model_identity


READINESS_VERSION = 1
READINESS_PREFIX = ".runonrunpod/readiness/v1/"


class ReadinessError(ValueError):
    """A readiness receipt is malformed or does not match its requirement."""


@dataclass(frozen=True)
class ModelReadinessReceipt:
    target_path: str
    identity: ModelIdentity
    verifier: str


def receipt_key(target_path: str) -> str:
    """Return a stable, safe object key without embedding a workflow path."""
    digest = hashlib.sha256(target_path.encode("utf-8")).hexdigest()
    return f"{READINESS_PREFIX}{digest}.json"


def receipt_dict(receipt: ModelReadinessReceipt) -> dict[str, object]:
    return {
        "readiness_version": READINESS_VERSION,
        "target_path": receipt.target_path,
        "sha256": receipt.identity.sha256,
        "size": receipt.identity.size,
        "verifier": receipt.verifier,
    }


def make_receipt(
    requirement: ModelRequirement,
    identity: ModelIdentity,
    verifier: str,
) -> ModelReadinessReceipt:
    if not isinstance(verifier, str) or not verifier:
        raise ReadinessError("receipt verifier must be a non-empty string")
    return ModelReadinessReceipt(requirement.target_path, identity, verifier)


def parse_receipt(value: object, requirement: ModelRequirement) -> ModelReadinessReceipt:
    if not isinstance(value, Mapping):
        raise ReadinessError("receipt must be an object")
    if value.get("readiness_version") != READINESS_VERSION:
        raise ReadinessError("unsupported readiness receipt version")
    if value.get("target_path") != requirement.target_path:
        raise ReadinessError("receipt target does not match requirement")
    try:
        identity = model_identity(value.get("sha256"), value.get("size"))
    except ResourcePlanError as error:
        raise ReadinessError(str(error)) from None
    verifier = value.get("verifier")
    if not isinstance(verifier, str) or not verifier:
        raise ReadinessError("receipt verifier must be a non-empty string")
    return ModelReadinessReceipt(requirement.target_path, identity, verifier)


def receipt_bytes(receipt: ModelReadinessReceipt) -> bytes:
    return json.dumps(
        receipt_dict(receipt), sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def load_matching_receipt(client, bucket: str, requirement: ModelRequirement) -> ModelReadinessReceipt | None:
    """Return a receipt only if its model object still has the recorded size.

    The CPU stager will be the cryptographic verifier. This local integration
    establishes the same completion barrier: an object is never considered
    ready merely because its path exists, and writing an object before its
    receipt means an interrupted transfer cannot be submitted to the GPU.
    """
    try:
        response = client.get_object(Bucket=bucket, Key=receipt_key(requirement.target_path))
    except KeyError:
        return None
    except Exception as error:
        response = getattr(error, "response", {})
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    try:
        raw = response["Body"].read()
        receipt = parse_receipt(json.loads(raw.decode("utf-8")), requirement)
    except (KeyError, TypeError, UnicodeDecodeError, json.JSONDecodeError, ReadinessError):
        return None
    try:
        head = client.head_object(Bucket=bucket, Key=requirement.target_path)
    except KeyError:
        return None
    except Exception as error:
        response = getattr(error, "response", {})
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode") if isinstance(response, dict) else None
        code = response.get("Error", {}).get("Code") if isinstance(response, dict) else None
        if status == 404 or code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    if head.get("ContentLength") != receipt.identity.size:
        return None
    return receipt


def clear_receipt(client, bucket: str, requirement: ModelRequirement) -> None:
    """Remove a stale completion marker before replacing model bytes."""
    client.delete_object(Bucket=bucket, Key=receipt_key(requirement.target_path))


def write_receipt(
    client,
    bucket: str,
    requirement: ModelRequirement,
    identity: ModelIdentity,
    verifier: str,
) -> None:
    """Publish readiness only after the object size matches the identity."""
    head = client.head_object(Bucket=bucket, Key=requirement.target_path)
    if head.get("ContentLength") != identity.size:
        raise ReadinessError(
            f"model object size does not match materialized identity for {requirement.target_path}"
        )
    receipt = make_receipt(requirement, identity, verifier)
    client.put_object(
        Bucket=bucket,
        Key=receipt_key(requirement.target_path),
        Body=receipt_bytes(receipt),
        ContentType="application/json",
    )
