"""Thin CPU-only Serverless stager for signed, immutable model requests."""

from __future__ import annotations

import hashlib
import os
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


def handler(job: dict) -> dict[str, object]:
    try:
        request = verify_signed_stage_request(job["input"].get("signed_request"), SIGNING_KEY)
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
