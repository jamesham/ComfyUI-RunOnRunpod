"""Managed-session configuration supplied by the ComfyUI plugin request.

The plugin user explicitly authorizes lifecycle mutations by supplying their
RunPod API key and managed profile in the settings UI. Do not use server
environment variables for normal managed-session operation: they hide required
configuration from the user and make desktop/remote ComfyUI setups diverge.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping

from .lifecycle import LifecycleError, ManagedProfile, SessionLifecycleService
from .runpod_adapter import RunPodLifecycleAdapter, Transport
from .session_store import SessionCoordinator


PROFILE_SETTING = "managedProfile"
RECIPE_SETTING = "managedRecipeId"
STATE_ROOT_SETTING = "managedStateRoot"
DEFAULT_RECIPE_ID = "default"


class ManagedSessionConfigError(LifecycleError):
    pass


def _settings(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ManagedSessionConfigError("managed-session settings must be an object")
    return value


def default_state_root() -> Path:
    """Keep durable state beside the plugin unless the UI selects another path."""
    return Path(__file__).resolve().parents[1] / ".runonrunpod"


def coordinator_from_settings(settings: Mapping[str, object]) -> SessionCoordinator:
    """Open user-selected local state, defaulting to a portable plugin-local path."""
    root = settings.get(STATE_ROOT_SETTING)
    if root in (None, ""):
        path = default_state_root()
    elif isinstance(root, str):
        path = Path(root)
    else:
        raise ManagedSessionConfigError("managed state folder must be a string")
    return SessionCoordinator(path)


def profile_from_settings(settings: Mapping[str, object]) -> ManagedProfile:
    """Parse the complete, non-secret managed endpoint profile from UI settings."""
    value = settings.get(PROFILE_SETTING)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ManagedSessionConfigError(f"managed profile JSON is invalid: {error.msg}") from None
    try:
        return ManagedProfile.from_dict(value)
    except LifecycleError as error:
        raise ManagedSessionConfigError(f"managed profile is invalid: {error}") from None


def _validate_v2_cpu_profile(profile: ManagedProfile) -> None:
    if not profile.cpu_template_id:
        raise ManagedSessionConfigError("managed profile CPU configuration requires template_id")
    if not profile.cpu_flavor_ids or profile.cpu_vcpu_count is None:
        raise ManagedSessionConfigError(
            "managed profile CPU configuration requires flavor_ids and vcpu_count for RunPod REST v2"
        )
    if profile.cpu_vcpu_count < 2 or profile.cpu_vcpu_count & (profile.cpu_vcpu_count - 1):
        raise ManagedSessionConfigError(
            "managed profile CPU vcpu_count must be a power of two and at least 2 for RunPod REST v2"
        )


def validate_v2_gpu_profile(profile: ManagedProfile) -> None:
    """Require the user-selected fields used for managed GPU creation."""
    if not profile.gpu_image or not profile.gpu_pool_ids:
        raise ManagedSessionConfigError(
            "managed profile GPU configuration requires image and pool_ids"
        )


def managed_recipe_from_settings(
    coordinator: SessionCoordinator, settings: Mapping[str, object],
) -> str:
    """Use an existing recipe or initialize an empty working-set recipe.

    Model additions are captured by the later recipe-restoration milestone. An
    empty recipe lets a user start the first disposable session entirely from
    the plugin UI today.
    """
    recipe_id = settings.get(RECIPE_SETTING, DEFAULT_RECIPE_ID)
    if not isinstance(recipe_id, str) or not recipe_id:
        raise ManagedSessionConfigError("managed recipe ID must be a non-empty string")
    if not coordinator.recipe_exists(recipe_id):
        try:
            from ..resource_plan import compile_model_resource_plan
        except ImportError:  # pragma: no cover - direct library use.
            from resource_plan import compile_model_resource_plan
        coordinator.save_recipe(recipe_id, compile_model_resource_plan({}, {}), {})
    return recipe_id


def managed_configuration_from_settings(
    settings: Mapping[str, object],
) -> tuple[SessionCoordinator, ManagedProfile, str]:
    """Load validated UI configuration without reading process environment."""
    values = _settings(settings)
    coordinator = coordinator_from_settings(values)
    profile = profile_from_settings(values)
    _validate_v2_cpu_profile(profile)
    validate_v2_gpu_profile(profile)
    recipe_id = managed_recipe_from_settings(coordinator, values)
    return coordinator, profile, recipe_id


def lifecycle_from_settings(
    settings: Mapping[str, object],
    *,
    transport: Transport | None = None,
) -> tuple[SessionLifecycleService, ManagedProfile, str]:
    """Build a mutating lifecycle service from explicit request-scoped settings."""
    values = _settings(settings)
    api_key = values.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        raise ManagedSessionConfigError("a RunPod API key is required")
    coordinator, profile, recipe_id = managed_configuration_from_settings(values)
    provider = RunPodLifecycleAdapter(api_key, allow_mutations=True, transport=transport)
    return SessionLifecycleService(coordinator, provider), profile, recipe_id
