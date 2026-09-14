"""RunPod HTTPS transport for CPU-volume artifact operations."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

try:
    from .cpu_artifact_contract import (
        CPU_ARTIFACT_PROTOCOL_VERSION,
        MAX_CHUNK_BYTES,
        CpuArtifactContractError,
        validate_artifact_request,
    )
except ImportError:  # pragma: no cover - direct worker-tool import fallback.
    from cpu_artifact_contract import (  # type: ignore
        CPU_ARTIFACT_PROTOCOL_VERSION,
        MAX_CHUNK_BYTES,
        CpuArtifactContractError,
        validate_artifact_request,
    )


class CpuArtifactError(RuntimeError):
    pass


async def _run_request(
    endpoint_id: str,
    api_key: str,
    artifact_request: object,
    *,
    timeout_seconds: float = 1_800,
    poll_interval_seconds: float = 1,
) -> dict[str, object]:
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise CpuArtifactError("CPU artifact endpoint ID is required")
    if not isinstance(api_key, str) or not api_key:
        raise CpuArtifactError("RunPod API key is required for CPU artifact transfer")
    try:
        request = validate_artifact_request(artifact_request)
        import aiohttp
    except CpuArtifactContractError as error:
        raise CpuArtifactError(str(error)) from None
    except ImportError:
        raise CpuArtifactError("CPU artifact transport requires aiohttp") from None
    headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "ComfyUI-RunOnRunpod"}
    deadline = time.monotonic() + timeout_seconds
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run", headers=headers,
            json={"input": {"artifact_request": request}},
        ) as response:
            submitted = await response.json()
        job_id = submitted.get("id") if isinstance(submitted, dict) else None
        if not isinstance(job_id, str) or not job_id:
            raise CpuArtifactError(f"CPU artifact submission failed: {submitted}")
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CpuArtifactError("CPU artifact transfer timed out while waiting for RunPod")
            await asyncio.sleep(min(poll_interval_seconds, remaining))
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}", headers=headers,
            ) as response:
                status = await response.json()
            state = status.get("status") if isinstance(status, dict) else None
            if state == "COMPLETED":
                output = status.get("output") if isinstance(status, dict) else None
                if not isinstance(output, dict) or output.get("status") != "success":
                    message = output.get("error") if isinstance(output, dict) else None
                    raise CpuArtifactError(f"CPU artifact operation failed: {message or output}")
                if output.get("operation_id") != request["operation_id"]:
                    raise CpuArtifactError("CPU artifact response operation ID does not match")
                if output.get("action") != request["action"] or output.get("target_path") != request["target_path"]:
                    raise CpuArtifactError("CPU artifact response does not match its request")
                return output
            if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
                raise CpuArtifactError(f"CPU artifact operation failed: {status.get('error') or state}")


def _request(
    operation_id: str,
    volume_binding: str,
    action: str,
    target_path: str,
    **values: object,
) -> dict[str, object]:
    return {
        "protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
        "operation_id": operation_id,
        "volume_binding": volume_binding,
        "action": action,
        "target_path": target_path,
        **values,
    }


async def upload_file(
    endpoint_id: str,
    api_key: str,
    volume_binding: str,
    operation_id: str,
    target_path: str,
    source_path: str,
) -> dict[str, object]:
    """Install a local file on the CPU-attached volume in bounded chunks."""
    digest = hashlib.sha256()
    size = 0
    with open(source_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    transfer_id = uuid.uuid4().hex
    offset = 0
    final_result: dict[str, object] | None = None
    with open(source_path, "rb") as source:
        while True:
            chunk = source.read(MAX_CHUNK_BYTES)
            complete = offset + len(chunk) == size
            if not chunk and offset != 0:
                break
            request = _request(
                operation_id, volume_binding, "write", target_path,
                transfer_id=transfer_id, offset=offset,
                data=base64.b64encode(chunk).decode("ascii"), complete=complete,
                expected_sha256=digest.hexdigest(), expected_size=size,
            )
            final_result = await _run_request(endpoint_id, api_key, request)
            offset += len(chunk)
            if complete or (
                final_result.get("sha256") == digest.hexdigest()
                and final_result.get("size") == size
            ):
                break
    if final_result is None:
        raise CpuArtifactError("CPU artifact upload did not submit a request")
    if final_result.get("sha256") != digest.hexdigest() or final_result.get("size") != size:
        raise CpuArtifactError("CPU artifact upload returned a mismatched identity")
    return final_result


async def download_file(
    endpoint_id: str,
    api_key: str,
    volume_binding: str,
    operation_id: str,
    target_path: str,
    destination: str,
) -> None:
    """Retrieve an artifact through the CPU endpoint and atomically publish it locally."""
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination_path.name}.", suffix=".part", dir=destination_path.parent)
    offset = 0
    try:
        with os.fdopen(descriptor, "wb") as local:
            while True:
                request = _request(
                    operation_id, volume_binding, "read", target_path,
                    offset=offset, length=MAX_CHUNK_BYTES,
                )
                result = await _run_request(endpoint_id, api_key, request)
                encoded = result.get("data")
                if not isinstance(encoded, str):
                    raise CpuArtifactError("CPU artifact read response has no data")
                try:
                    chunk = base64.b64decode(encoded.encode("ascii"), validate=True)
                except Exception:
                    raise CpuArtifactError("CPU artifact read response data is invalid") from None
                if result.get("offset") != offset or len(chunk) > MAX_CHUNK_BYTES:
                    raise CpuArtifactError("CPU artifact read response has an invalid offset or length")
                local.write(chunk)
                offset += len(chunk)
                if result.get("complete") is True:
                    local.flush()
                    os.fsync(local.fileno())
                    break
                if not chunk:
                    raise CpuArtifactError("CPU artifact read returned an empty non-final chunk")
        os.replace(temporary, destination_path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


async def delete_file(
    endpoint_id: str,
    api_key: str,
    volume_binding: str,
    operation_id: str,
    target_path: str,
) -> None:
    request = _request(operation_id, volume_binding, "delete", target_path)
    await _run_request(endpoint_id, api_key, request)
