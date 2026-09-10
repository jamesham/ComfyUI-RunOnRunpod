"""Operator-only managed session commands.

This module intentionally has no ComfyUI route registration. It is invoked by
an administrator with server-side environment variables, keeping lifecycle
authorization and the RunPod API key out of browser-controlled settings.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Mapping, Sequence

from .lifecycle import LifecycleError, ManagedProfile, SessionLifecycleService
from .runpod_adapter import RunPodLifecycleAdapter, Transport
from .session_store import CoordinatorError, SessionCoordinator


ENABLE_ENV = "RUNONRUNPOD_MANAGED_LIFECYCLE"
ENABLE_VALUE = "enabled"
ROOT_ENV = "RUNONRUNPOD_COORDINATOR_ROOT"
API_KEY_ENV = "RUNPOD_API_KEY"
PROFILE_ENV = "RUNONRUNPOD_MANAGED_PROFILE_PATH"


class ManagedSessionConfigError(LifecycleError):
    pass


def _environment(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def coordinator_from_environment(environ: Mapping[str, str] | None = None) -> SessionCoordinator:
    """Open only the explicitly configured local coordinator state root."""
    root = _environment(environ).get(ROOT_ENV)
    if not isinstance(root, str) or not root:
        raise ManagedSessionConfigError(f"{ROOT_ENV} must name the local coordinator state root")
    if not Path(root).is_dir():
        raise ManagedSessionConfigError(f"{ROOT_ENV} must name an existing local coordinator state root")
    return SessionCoordinator(root)


def profile_from_environment(environ: Mapping[str, str] | None = None) -> ManagedProfile:
    """Read a validated profile from an administrator-owned JSON file."""
    profile_path = _environment(environ).get(PROFILE_ENV)
    if not isinstance(profile_path, str) or not profile_path:
        raise ManagedSessionConfigError(f"{PROFILE_ENV} must name an operator-owned profile file")
    try:
        value = json.loads(Path(profile_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ManagedSessionConfigError(f"cannot read managed profile: {error}") from None
    try:
        return ManagedProfile.from_dict(value)
    except LifecycleError as error:
        raise ManagedSessionConfigError(f"managed profile is invalid: {error}") from None


def lifecycle_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    transport: Transport | None = None,
) -> tuple[SessionLifecycleService, ManagedProfile]:
    """Build the sole mutating lifecycle path after explicit operator opt-in."""
    values = _environment(environ)
    if values.get(ENABLE_ENV) != ENABLE_VALUE:
        raise ManagedSessionConfigError(
            f"set {ENABLE_ENV}={ENABLE_VALUE} to authorize managed lifecycle mutations"
        )
    api_key = values.get(API_KEY_ENV)
    if not isinstance(api_key, str) or not api_key:
        raise ManagedSessionConfigError(f"{API_KEY_ENV} is required for managed lifecycle mutations")
    profile = profile_from_environment(values)
    if not profile.cpu_template_id:
        raise ManagedSessionConfigError("managed profile CPU configuration requires template_id")
    coordinator = coordinator_from_environment(values)
    provider = RunPodLifecycleAdapter(api_key, allow_mutations=True, transport=transport)
    return SessionLifecycleService(coordinator, provider), profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m coordinator.managed_sessions",
        description="Operator-only lifecycle control for disposable CPU-staging sessions.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="create a managed session volume and CPU endpoint")
    start.add_argument("recipe_id")
    start.add_argument("--session-id")
    recover = commands.add_parser("recover", help="reconcile resources recorded for a managed session")
    recover.add_argument("session_id")
    end = commands.add_parser("end", help="delete recorded endpoint(s) and session volume")
    end.add_argument("session_id")
    end.add_argument(
        "--outputs-retrieved", action="store_true",
        help="required acknowledgement until durable output retrieval records are wired",
    )
    status = commands.add_parser("status", help="show only the durable local session record")
    status.add_argument("session_id")
    return parser


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    """Run a command and emit JSON; provider operations remain opt-in by env."""
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "status":
            result = coordinator_from_environment(environ).get_session(arguments.session_id)
        else:
            if arguments.command == "end" and not arguments.outputs_retrieved:
                raise ManagedSessionConfigError(
                    "end requires --outputs-retrieved until durable output retrieval checks are available"
                )
            service, profile = lifecycle_from_environment(environ)
            if arguments.command == "start":
                result = service.start(arguments.recipe_id, profile, session_id=arguments.session_id)
            elif arguments.command == "recover":
                result = service.recover(arguments.session_id, profile)
            else:
                result = service.end(arguments.session_id)
    except (CoordinatorError, LifecycleError) as error:
        print(f"managed session command failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
