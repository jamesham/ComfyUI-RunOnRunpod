"""RunPod transport for a coordinator-signed CPU staging operation."""

from __future__ import annotations

import asyncio

import aiohttp

from .cpu_staging_contract import CpuStagingContractError, unsigned_stage_payload, validate_stage_result


class CpuStagerError(RuntimeError):
    pass


async def stage_models(
    endpoint_id: str,
    api_key: str,
    signed_request: object,
    on_progress=None,
) -> dict[str, object]:
    """Run a signed CPU stage request and return its validated final result."""
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise CpuStagerError("CPU staging endpoint ID is required")
    request = unsigned_stage_payload(signed_request)
    headers = {"Authorization": f"Bearer {api_key}"}
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.runpod.ai/v2/{endpoint_id}/run",
            headers=headers,
            json={"input": {"signed_request": signed_request}},
        ) as response:
            submitted = await response.json()
        job_id = submitted.get("id") if isinstance(submitted, dict) else None
        if not job_id:
            raise CpuStagerError(f"CPU staging submission failed: {submitted}")

        while True:
            await asyncio.sleep(1)
            async with session.get(
                f"https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}", headers=headers,
            ) as response:
                status = await response.json()
            state = status.get("status") if isinstance(status, dict) else None
            output = status.get("output") if isinstance(status, dict) else None
            if state == "IN_PROGRESS" and isinstance(output, dict) and on_progress:
                on_progress(output)
            if state == "COMPLETED":
                try:
                    return validate_stage_result(output, request)
                except CpuStagingContractError as error:
                    raise CpuStagerError(str(error)) from None
            if state in ("FAILED", "CANCELLED", "TIMED_OUT"):
                message = status.get("error") or status.get("message") or state
                raise CpuStagerError(f"CPU staging failed: {message}")
