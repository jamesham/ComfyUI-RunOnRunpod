"""Operator-only managed-session command and configuration tests."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from coordinator import ManagedSessionConfigError
from coordinator.managed_sessions import (
    API_KEY_ENV,
    ENABLE_ENV,
    ENABLE_VALUE,
    PROFILE_ENV,
    ROOT_ENV,
    lifecycle_from_environment,
    main,
)


class FakeLifecycleService:
    def __init__(self):
        self.calls = []

    def start(self, recipe_id, profile, *, session_id=None):
        self.calls.append(("start", recipe_id, profile.profile_id, session_id))
        return {"session_id": session_id or "created", "state": "preparing"}

    def recover(self, session_id, profile):
        self.calls.append(("recover", session_id, profile.profile_id))
        return {"session_id": session_id, "state": "preparing"}

    def end(self, session_id):
        self.calls.append(("end", session_id))
        return {"session_id": session_id, "state": "closed"}


class ManagedSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile_path = Path(self.directory.name) / "operator-profile.json"
        self.profile_path.write_text(json.dumps({
            "profile_version": 1, "profile_id": "operator-profile", "data_center": "dc-1",
            "volume": {"size_gb": 100},
            "cpu": {"image": "cpu@sha256:abc", "template_id": "cpu-template"},
        }), encoding="utf-8")
        self.environment = {
            ROOT_ENV: self.directory.name, API_KEY_ENV: "server-only-key",
            PROFILE_ENV: str(self.profile_path), ENABLE_ENV: ENABLE_VALUE,
        }

    def test_mutating_service_requires_explicit_operator_opt_in(self):
        environment = dict(self.environment)
        environment.pop(ENABLE_ENV)
        with self.assertRaisesRegex(ManagedSessionConfigError, ENABLE_ENV):
            lifecycle_from_environment(environment, transport=lambda *_: (500, None))

    def test_state_root_must_already_exist(self):
        environment = dict(self.environment)
        environment[ROOT_ENV] = str(Path(self.directory.name) / "missing-state")
        with self.assertRaisesRegex(ManagedSessionConfigError, "existing local coordinator"):
            lifecycle_from_environment(environment, transport=lambda *_: (500, None))

    def test_mutating_service_loads_operator_profile_and_enables_adapter(self):
        service, profile = lifecycle_from_environment(
            self.environment, transport=lambda *_: (500, None),
        )
        self.assertEqual(profile.profile_id, "operator-profile")
        self.assertTrue(service.provider.allow_mutations)
        self.assertEqual(service.provider.api_key, "server-only-key")

    def test_cli_start_delegates_without_browser_controlled_configuration(self):
        service, profile = lifecycle_from_environment(
            self.environment, transport=lambda *_: (500, None),
        )
        fake = FakeLifecycleService()
        output = io.StringIO()
        with patch("coordinator.managed_sessions.lifecycle_from_environment", return_value=(fake, profile)):
            with contextlib.redirect_stdout(output):
                code = main(["start", "recipe-1", "--session-id", "session-1"], environ={})
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [("start", "recipe-1", "operator-profile", "session-1")])
        self.assertEqual(json.loads(output.getvalue()), {"session_id": "session-1", "state": "preparing"})

    def test_cli_end_requires_output_retrieval_acknowledgement(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            code = main(["end", "session-1"], environ=self.environment)
        self.assertEqual(code, 2)
        self.assertIn("--outputs-retrieved", errors.getvalue())

    def test_invalid_profile_is_rejected_before_any_provider_request(self):
        self.profile_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ManagedSessionConfigError, "profile is invalid"):
            lifecycle_from_environment(self.environment, transport=lambda *_: (500, None))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
