"""Opt-in live smoke test for a disposable RunPod CPU staging session.

This script intentionally cannot run without ``--live``. It creates billable
RunPod resources and always attempts to delete only the exact resources recorded
by the coordinator. It does not print API keys, HMAC values, or secret values.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from types import SimpleNamespace
from typing import Sequence

from coordinator import ManagedProfile, RunPodLifecycleAdapter, SessionCoordinator, SessionLifecycleService
from cpu_stager_client import stage_models
from resource_plan import ModelIdentity, compile_model_resource_plan


_SECRET_NAME = re.compile(r"^[A-Za-z0-9_-]+$")


def _secret_reference(name: str) -> str:
    if not _SECRET_NAME.fullmatch(name):
        raise ValueError("RunPod secret names may contain only letters, digits, _ and -")
    return f"{{{{ RUNPOD_SECRET_{name} }}}}"


def _environment_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"environment variable {name} is required")
    return value


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create, exercise, inspect, and delete a temporary RunPod CPU stager session.",
    )
    parser.add_argument("--live", action="store_true", help="required acknowledgement of billable provider operations")
    parser.add_argument("--pause-after-create", action="store_true", help="wait for Enter after creation before staging and cleanup")
    parser.add_argument("--api-key-env", default="RUNPOD_API_KEY", help="environment variable holding the RunPod API key")
    parser.add_argument("--signing-key-env", default="RUNONRUNPOD_CPU_STAGING_SIGNING_KEY", help="environment variable holding the CPU HMAC value")
    parser.add_argument("--data-center", required=True, help="RunPod data-center ID for volume and CPU endpoint")
    parser.add_argument("--cpu-template-id", required=True, help="preconfigured worker-cpu Serverless template ID")
    parser.add_argument("--cpu-image", required=True, help="immutable worker-cpu image reference recorded in the profile")
    parser.add_argument("--hmac-secret-name", required=True, help="RunPod stored-secret name mapped to STAGING_REQUEST_HMAC_KEY")
    parser.add_argument("--provider-secret-name", required=True, help="RunPod stored-secret name for the selected provider token")
    parser.add_argument("--provider", choices=("hf", "civitai"), default="hf", help="source provider auth type")
    parser.add_argument("--download-url", required=True, help="HTTPS model URL served by the selected provider")
    parser.add_argument("--sha256", required=True, help="expected SHA-256 of the one test download")
    parser.add_argument("--size", required=True, type=int, help="expected byte size of the one test download")
    parser.add_argument("--model-name", default="runpod-cpu-stager-smoke.bin", help="temporary model filename")
    parser.add_argument("--volume-size-gb", type=int, default=10)
    parser.add_argument("--cpu-flavor-id", action="append", default=[], help="optional CPU flavor; may be repeated")
    parser.add_argument("--vcpu-count", type=int)
    parser.add_argument("--idle-timeout-seconds", type=int, default=5)
    parser.add_argument("--execution-timeout-seconds", type=int, default=1800)
    parser.add_argument("--stage-timeout-seconds", type=int, default=1500)
    parser.add_argument("--state-root", help="optional existing local directory for durable smoke-test records")
    arguments = parser.parse_args(argv)
    if not arguments.live:
        parser.error("--live is required because this test creates and deletes billable RunPod resources")
    if arguments.size < 1 or arguments.volume_size_gb < 1 or arguments.execution_timeout_seconds < 1:
        parser.error("size, volume size, and execution timeout must be positive")
    if arguments.stage_timeout_seconds < 1 or arguments.stage_timeout_seconds > arguments.execution_timeout_seconds:
        parser.error("stage timeout must be positive and no greater than endpoint execution timeout")
    if "/" in arguments.model_name or "\\" in arguments.model_name or not arguments.model_name:
        parser.error("model name must be a single filename")
    if not arguments.download_url.startswith("https://"):
        parser.error("download URL must use HTTPS")
    return arguments


def _profile(arguments: argparse.Namespace, session_id: str) -> ManagedProfile:
    provider_variable = "HF_TOKEN" if arguments.provider == "hf" else "CIVITAI_API_KEY"
    return ManagedProfile(
        profile_id=f"smoke-profile-{session_id}",
        data_center=arguments.data_center,
        volume_size_gb=arguments.volume_size_gb,
        cpu_image=arguments.cpu_image,
        cpu_template_id=arguments.cpu_template_id,
        cpu_flavor_ids=tuple(arguments.cpu_flavor_id),
        cpu_vcpu_count=arguments.vcpu_count,
        idle_timeout_seconds=arguments.idle_timeout_seconds,
        execution_timeout_ms=arguments.execution_timeout_seconds * 1000,
        cpu_environment=(
            ("STAGING_REQUEST_HMAC_KEY", _secret_reference(arguments.hmac_secret_name)),
            (provider_variable, _secret_reference(arguments.provider_secret_name)),
        ),
    )


def _save_recipe(coordinator: SessionCoordinator, arguments: argparse.Namespace, session_id: str) -> tuple[str, dict[str, object]]:
    workflow = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": arguments.model_name}},
    }
    plan = compile_model_resource_plan(workflow, {"CheckpointLoaderSimple": ("ckpt_name", "checkpoints")})
    requirement = plan.requirements[0]
    descriptor = {
        "url": arguments.download_url, "auth": arguments.provider,
        "expected_sha256": arguments.sha256, "expected_size": arguments.size,
    }
    recipe_id = f"smoke-recipe-{session_id}"
    coordinator.save_recipe(recipe_id, plan, {
        requirement.target_path: SimpleNamespace(
            identity=ModelIdentity(arguments.sha256, arguments.size), local_path=None,
            descriptor=descriptor,
        ),
    })
    return recipe_id, {**descriptor, "dest_path": requirement.target_path}


def _safe_summary(session: dict[str, object]) -> dict[str, object]:
    bindings = session.get("bindings") if isinstance(session.get("bindings"), dict) else {}
    return {
        "session_id": session.get("session_id"), "state": session.get("state"),
        "volume_id": bindings.get("volume_id"), "cpu_endpoint_id": bindings.get("cpu_endpoint_id"),
    }


def run(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    api_key = _environment_value(arguments.api_key_env)
    signing_key = _environment_value(arguments.signing_key_env)
    session_id = f"smoke-{uuid.uuid4().hex[:20]}"
    temporary_root: tempfile.TemporaryDirectory[str] | None = None
    if arguments.state_root:
        root = Path(arguments.state_root)
        if not root.is_dir():
            raise ValueError("--state-root must name an existing local directory")
    else:
        temporary_root = tempfile.TemporaryDirectory(prefix="runonrunpod-smoke-")
        root = Path(temporary_root.name)

    coordinator = SessionCoordinator(root)
    provider = RunPodLifecycleAdapter(api_key, allow_mutations=True)
    service = SessionLifecycleService(coordinator, provider)
    created = False
    outcome = 1
    try:
        profile = _profile(arguments, session_id)
        recipe_id, download = _save_recipe(coordinator, arguments, session_id)
        session = service.start(recipe_id, profile, session_id=session_id)
        created = True
        print("CREATED", _safe_summary(session), flush=True)
        print("Secret mappings requested: STAGING_REQUEST_HMAC_KEY and "
              f"{'HF_TOKEN' if arguments.provider == 'hf' else 'CIVITAI_API_KEY'}", flush=True)
        if arguments.pause_after_create:
            input("Inspect RunPod now. Press Enter to stage one download and clean up these resources. ")

        envelope = coordinator.authorize_stage(session_id, "smoke-stage", [download], signing_key)
        endpoint_id = session["bindings"]["cpu_endpoint_id"]
        result = asyncio.run(stage_models(
            endpoint_id, api_key, envelope, timeout_seconds=arguments.stage_timeout_seconds,
        ))
        coordinator.record_stage_result(session_id, "smoke-stage", result)
        print("STAGED", {"target_path": download["dest_path"], "sha256": arguments.sha256, "size": arguments.size}, flush=True)
        outcome = 0
    except Exception as error:
        print(f"SMOKE TEST FAILED: {error}", file=sys.stderr, flush=True)
    finally:
        if created:
            try:
                closed = service.end(session_id)
                print("CLEANED", _safe_summary(closed), flush=True)
            except Exception as error:
                print(f"CLEANUP FAILED for session {session_id}: {error}", file=sys.stderr, flush=True)
                outcome = 1
        if temporary_root is not None:
            temporary_root.cleanup()
    return outcome


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
