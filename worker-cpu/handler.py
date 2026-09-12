"""Thin CPU-only Serverless stager for signed, immutable model requests."""

from __future__ import annotations

import hashlib
import os
import base64
from pathlib import PurePosixPath
from contextlib import contextmanager
import fcntl

import requests
import runpod

from cpu_staging_contract import (
    CPU_STAGING_PROTOCOL_VERSION,
    CpuStagingContractError,
    verify_signed_stage_request,
)
from cpu_artifact_contract import (
    CPU_ARTIFACT_PROTOCOL_VERSION,
    CpuArtifactContractError,
    verify_signed_artifact_request,
)


VOLUME_DIR = os.environ.get("STAGING_VOLUME_DIR", "/runpod-volume")
VOLUME_BINDING = os.environ.get("STAGING_VOLUME_BINDING", "")
SIGNING_KEY = os.environ.get("STAGING_REQUEST_HMAC_KEY", "")
CHUNK_SIZE = 4 * 1024 * 1024


class StageError(RuntimeError):
    pass


def _destination(target_path: str) -> str:
    relative = PurePosixPath(target_path)
    target = os.path.realpath(os.path.join(VOLUME_DIR, *relative.parts))
    volume = os.path.realpath(VOLUME_DIR)
    if not target.startswith(volume + os.sep):
        raise StageError("target escapes staging volume")
    return target


def _headers(auth: str) -> dict[str, str]:
    headers = {"User-Agent": "ComfyUI-RunOnRunpod"}
    if auth == "hf":
        token = os.environ.get("HF_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif auth == "civitai":
        key = os.environ.get("CIVITAI_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    return headers


@contextmanager
def _target_lock(destination: str):
    """Serialize competing CPU jobs that publish the same volume target."""
    lock_path = f"{destination}.stage.lock"
    with open(lock_path, "a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _stage_one(model: dict) -> dict[str, object]:
    destination = _destination(model["target_path"])
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    with _target_lock(destination):
        if os.path.isfile(destination):
            actual_sha256, actual_size = _digest_file(destination)
            if actual_sha256 == model["expected_sha256"] and actual_size == model["expected_size"]:
                return {
                    "target_path": model["target_path"], "status": "done",
                    "sha256": actual_sha256, "size": actual_size, "cache_hit": True,
                }
        partial = f"{destination}.stage-{os.getpid()}.part"
        digest = hashlib.sha256()
        written = 0
        try:
            with requests.get(
                model["url"], headers=_headers(model["auth"]), stream=True,
                allow_redirects=True, timeout=(30, 300),
            ) as response:
                if response.status_code != 200:
                    raise StageError(f"source returned HTTP {response.status_code}")
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                        if chunk:
                            handle.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != model["expected_sha256"] or written != model["expected_size"]:
                raise StageError("downloaded bytes do not match the signed identity")
            os.replace(partial, destination)
            return {
                "target_path": model["target_path"], "status": "done",
                "sha256": actual_sha256, "size": written,
            }
        except Exception:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise


def _digest_file(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _artifact_write(request: dict[str, object]) -> dict[str, object]:
    destination = _destination(str(request["target_path"]))
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    transfer_id = str(request["transfer_id"])
    partial = f"{destination}.artifact-{transfer_id}.part"
    chunk = base64.b64decode(str(request["data"]).encode("ascii"), validate=True)
    with _target_lock(destination):
        if request["offset"] == 0 and os.path.isfile(destination):
            actual_sha256, actual_size = _digest_file(destination)
            if actual_sha256 == request["expected_sha256"] and actual_size == request["expected_size"]:
                return {
                    "artifact_protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
                    "operation_id": request["operation_id"], "status": "success",
                    "action": "write", "target_path": request["target_path"],
                    "offset": actual_size, "sha256": actual_sha256, "size": actual_size,
                    "cache_hit": True,
                }
        actual_offset = os.path.getsize(partial) if os.path.exists(partial) else 0
        if actual_offset != request["offset"]:
            raise StageError("artifact write offset does not match the stored partial")
        with open(partial, "ab") as handle:
            handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        written = actual_offset + len(chunk)
        if not request["complete"]:
            return {
                "artifact_protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
                "operation_id": request["operation_id"], "status": "success",
                "action": "write", "target_path": request["target_path"], "offset": written,
            }
        actual_sha256, actual_size = _digest_file(partial)
        if actual_sha256 != request["expected_sha256"] or actual_size != request["expected_size"]:
            try:
                os.remove(partial)
            except OSError:
                pass
            raise StageError("artifact bytes do not match the signed identity")
        os.replace(partial, destination)
        return {
            "artifact_protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
            "operation_id": request["operation_id"], "status": "success",
            "action": "write", "target_path": request["target_path"],
            "offset": written, "sha256": actual_sha256, "size": actual_size,
        }


def _artifact_read(request: dict[str, object]) -> dict[str, object]:
    source = _destination(str(request["target_path"]))
    try:
        size = os.path.getsize(source)
    except OSError as error:
        raise StageError("artifact source is unavailable") from error
    offset = int(request["offset"])
    if offset > size:
        raise StageError("artifact read offset exceeds file size")
    with open(source, "rb") as handle:
        handle.seek(offset)
        chunk = handle.read(int(request["length"]))
    return {
        "artifact_protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
        "operation_id": request["operation_id"], "status": "success",
        "action": "read", "target_path": request["target_path"], "offset": offset,
        "data": base64.b64encode(chunk).decode("ascii"), "complete": offset + len(chunk) >= size,
        "size": size,
    }


def _artifact_delete(request: dict[str, object]) -> dict[str, object]:
    destination = _destination(str(request["target_path"]))
    with _target_lock(destination):
        try:
            os.remove(destination)
        except FileNotFoundError:
            pass
    return {
        "artifact_protocol_version": CPU_ARTIFACT_PROTOCOL_VERSION,
        "operation_id": request["operation_id"], "status": "success",
        "action": "delete", "target_path": request["target_path"],
    }


def _handle_artifact_request(job: dict, envelope: object) -> dict[str, object]:
    try:
        request = verify_signed_artifact_request(envelope, SIGNING_KEY)
        if request["volume_binding"] != VOLUME_BINDING:
            raise StageError("signed artifact request volume binding does not match deployment")
        if request["action"] == "write":
            return _artifact_write(request)
        if request["action"] == "read":
            return _artifact_read(request)
        return _artifact_delete(request)
    except (CpuArtifactContractError, StageError, OSError, ValueError) as error:
        return {"status": "failed", "error": str(error)}


def handler(job: dict) -> dict[str, object]:
    job_input = job.get("input")
    if not isinstance(job_input, dict):
        return {"status": "failed", "error": "job input is invalid"}
    if "signed_artifact_request" in job_input:
        return _handle_artifact_request(job, job_input.get("signed_artifact_request"))
    try:
        request = verify_signed_stage_request(job_input.get("signed_request"), SIGNING_KEY)
        if request["volume_binding"] != VOLUME_BINDING:
            raise StageError("signed request volume binding does not match deployment")
    except (KeyError, CpuStagingContractError, StageError) as error:
        return {"status": "failed", "error": str(error)}

    results = []
    for index, model in enumerate(request["models"]):
        runpod.serverless.progress_update(job, {
            "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
            "operation_id": request["operation_id"], "state": "staging",
            "current_index": index, "target_path": model["target_path"], "results": results,
        })
        try:
            results.append(_stage_one(model))
        except Exception as error:
            results.append({"target_path": model["target_path"], "status": "failed", "error": str(error)})
            return {
                "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
                "operation_id": request["operation_id"], "status": "failed", "results": results,
            }

    return {
        "protocol_version": CPU_STAGING_PROTOCOL_VERSION,
        "operation_id": request["operation_id"], "status": "success", "results": results,
    }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
