"""RunPod transport for a coordinator-signed CPU staging operation."""

from __future__ import annotations

import asyncio
import time

try:  # Plugin package import; direct integration-harness import fallback.
    from .cpu_staging_contract import CpuStagingContractError, unsigned_stage_payload, validate_stage_result
except ImportError:  # pragma: no cover - exercised by the standalone harness.
    from cpu_staging_contract import CpuStagingContractError, unsigned_stage_payload, validate_stage_result


class CpuStagerError(RuntimeError):
    pass


async def stage_models(
    endpoint_id: str,
    api_key: str,
    signed_request: object,
    on_progress=None,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = 1,
) -> dict[str, object]:
    """Run a signed CPU stage request and return its validated final result."""
    if not isinstance(endpoint_id, str) or not endpoint_id:
        raise CpuStagerError("CPU staging endpoint ID is required")
    if timeout_seconds is not None and timeout_seconds <= 0:
        raise CpuStagerError("CPU staging timeout must be positive")
    if poll_interval_seconds <= 0:
        raise CpuStagerError("CPU staging poll interval must be positive")
    try:
        import aiohttp
    except ImportError:
        raise CpuStagerError("CPU staging transport requires aiohttp") from None
    request = unsigned_stage_payload(signed_request)
    headers = {"Authorization": f"Bearer {api_key}"}
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
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
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CpuStagerError("CPU staging timed out while waiting for RunPod")
                await asyncio.sleep(min(poll_interval_seconds, remaining))
            else:
                await asyncio.sleep(poll_interval_seconds)
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
