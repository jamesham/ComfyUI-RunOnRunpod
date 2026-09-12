"""Hermetic tests for the ComfyUI managed-session lifecycle bridge."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_preparation_gate import load_routes


class Request:
    def __init__(self, value):
        self.value = value

    async def json(self):
        return self.value


def session(state="preparing"):
    return {
        "session_id": "session-1",
        "state": state,
        "profile_id": "profile-1",
        "recipe_id": "recipe-1",
        "bindings": {
            "volume_id": "volume-1",
            "volume_binding": "volume-1",
            "cpu_endpoint_id": "cpu-1",
            "gpu_endpoint_id": None,
        },
    }


async def inline_to_thread(function, *args, **kwargs):
    """Keep route tests deterministic without creating process-wide executors."""
    return function(*args, **kwargs)


class ManagedSessionRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes = load_routes()
        self.routes._active_prep_sessions.clear()
        self.routes._active_job_sessions.clear()
        self.coordinator = Mock()
        self.profile = SimpleNamespace(profile_id="profile-1")
        self.service = SimpleNamespace(
            coordinator=self.coordinator,
            start=Mock(return_value=session()),
            recover=Mock(return_value=session()),
            end=Mock(return_value=session("closed")),
        )
        self.settings = {
            "stagingMode": "cpu",
            "apiKey": "request-only-key",
            "endpointId": "gpu-1",
        }

    async def test_start_uses_server_recipe_and_browser_session_id(self):
        self.coordinator.session_exists.return_value = False
        with patch.object(
            self.routes, "_managed_lifecycle_for_settings",
            return_value=(self.service, self.profile, "recipe-1"),
        ), patch.object(self.routes.asyncio, "to_thread", new=inline_to_thread):
            result = await self.routes.start_managed_session(Request({
                "settings": self.settings, "session_id": "session-1",
            }))

        self.service.start.assert_called_once_with(
            "recipe-1", self.profile, session_id="session-1",
        )
        self.assertEqual(result["managedSessionId"], "session-1")
        self.assertEqual(result["volumeBinding"], "volume-1")
        self.assertNotIn("request-only-key", str(result))

    async def test_start_rejects_gpu_mode_without_calling_lifecycle(self):
        result = await self.routes.start_managed_session(Request({
            "settings": {**self.settings, "stagingMode": "gpu"},
            "session_id": "session-1",
        }))
        self.assertIn("CPU staging mode", result["error"])
        self.service.start.assert_not_called()

    async def test_retry_reconciles_the_same_recorded_session(self):
        self.coordinator.session_exists.return_value = True
        self.coordinator.get_session.return_value = session("recoverable")
        with patch.object(
            self.routes, "_managed_lifecycle_for_settings",
            return_value=(self.service, self.profile, "recipe-1"),
        ), patch.object(self.routes.asyncio, "to_thread", new=inline_to_thread):
            result = await self.routes.start_managed_session(Request({
                "settings": self.settings, "session_id": "session-1",
            }))

        self.service.start.assert_not_called()
        self.service.recover.assert_called_once_with("session-1", self.profile)
        self.assertEqual(result["cpuEndpointId"], "cpu-1")

    async def test_end_refuses_while_the_session_has_an_active_job(self):
        self.routes._active_job_sessions["job-1"] = "session-1"
        result = await self.routes.end_managed_session(Request({
            "settings": self.settings,
            "session_id": "session-1",
            "outputs_retrieved": True,
        }))
        self.assertIn("active", result["error"])
        self.service.end.assert_not_called()

    async def test_end_requires_explicit_output_acknowledgement(self):
        result = await self.routes.end_managed_session(Request({
            "settings": self.settings, "session_id": "session-1",
        }))
        self.assertIn("output-retrieval acknowledgement", result["error"])

    async def test_end_deletes_only_the_authorized_recorded_session(self):
        self.coordinator.get_session.return_value = session("ready")
        with patch.object(
            self.routes, "_managed_lifecycle_for_settings",
            return_value=(self.service, self.profile, "recipe-1"),
        ), patch.object(self.routes.asyncio, "to_thread", new=inline_to_thread):
            result = await self.routes.end_managed_session(Request({
                "settings": self.settings,
                "session_id": "session-1",
                "outputs_retrieved": True,
            }))

        self.service.end.assert_called_once_with("session-1")
        self.assertEqual(result["state"], "closed")

    async def test_end_refuses_a_session_from_another_server_profile(self):
        self.coordinator.get_session.return_value = {
            **session("ready"), "profile_id": "other-profile",
        }
        with patch.object(
            self.routes, "_managed_lifecycle_for_settings",
            return_value=(self.service, self.profile, "recipe-1"),
        ), patch.object(self.routes.asyncio, "to_thread", new=inline_to_thread):
            result = await self.routes.end_managed_session(Request({
                "settings": self.settings,
                "session_id": "session-1",
                "outputs_retrieved": True,
            }))

        self.assertIn("does not belong", result["error"])
        self.service.end.assert_not_called()

    async def test_config_response_contains_no_environment_or_secret_material(self):
        profile = SimpleNamespace(
            profile_id="profile-1", data_center="dc-1", volume_size_gb=20,
        )
        with patch.object(
            self.routes, "managed_configuration_from_environment",
            return_value=(self.coordinator, profile, "recipe-1"),
        ), patch.dict(
            self.routes.os.environ,
            {self.routes.MANAGED_SIGNING_KEY_ENV: "do-not-return-this"},
            clear=False,
        ):
            result = await self.routes.managed_session_config(None)

        self.assertTrue(result["configured"])
        self.assertEqual(result["dataCenter"], "dc-1")
        self.assertNotIn("do-not-return-this", str(result))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
