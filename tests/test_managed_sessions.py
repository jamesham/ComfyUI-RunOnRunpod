"""Tests for request-scoped managed-session configuration."""

import json
import tempfile
import unittest

from coordinator import ManagedSessionConfigError
from coordinator.managed_sessions import (
    default_state_root,
    lifecycle_from_settings,
    managed_configuration_from_settings,
)


class ManagedSessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.profile = {
            "profile_version": 1, "profile_id": "creative-session", "data_center": "dc-1",
            "volume": {"size_gb": 100},
            "cpu": {
                "image": "cpu@sha256:abc", "template_id": "cpu-template",
                "flavor_ids": ["cpu3c"], "vcpu_count": 4,
                "environment": {},
            },
            "gpu": {"image": "gpu@sha256:def", "pool_ids": ["ADA_24"], "disk_gb": 50},
        }
        self.settings = {
            "apiKey": "request-only-key",
            "managedProfile": json.dumps(self.profile),
            "managedRecipeId": "recipe-1",
            "managedStateRoot": self.directory.name,
        }

    def test_lifecycle_uses_the_request_key_and_enables_mutations(self):
        service, profile, recipe_id = lifecycle_from_settings(
            self.settings, transport=lambda *_: (500, None),
        )
        self.assertEqual(profile.profile_id, "creative-session")
        self.assertEqual(recipe_id, "recipe-1")
        self.assertTrue(service.provider.allow_mutations)
        self.assertEqual(service.provider.api_key, "request-only-key")

    def test_lifecycle_rejects_a_missing_request_key(self):
        with self.assertRaisesRegex(ManagedSessionConfigError, "RunPod API key"):
            lifecycle_from_settings({**self.settings, "apiKey": ""}, transport=lambda *_: (500, None))

    def test_configuration_initializes_an_empty_recipe_in_user_state(self):
        coordinator, profile, recipe_id = managed_configuration_from_settings(self.settings)
        self.assertEqual(recipe_id, "recipe-1")
        self.assertEqual(profile.profile_id, "creative-session")
        self.assertEqual(coordinator.get_recipe(recipe_id)["recipe_id"], recipe_id)

    def test_configuration_rejects_invalid_profile_json(self):
        with self.assertRaisesRegex(ManagedSessionConfigError, "profile JSON is invalid"):
            managed_configuration_from_settings({**self.settings, "managedProfile": "{"})

    def test_configuration_requires_v2_cpu_values(self):
        profile = json.loads(self.settings["managedProfile"])
        profile["cpu"].pop("flavor_ids")
        with self.assertRaisesRegex(ManagedSessionConfigError, "flavor_ids and vcpu_count"):
            managed_configuration_from_settings({**self.settings, "managedProfile": json.dumps(profile)})

    def test_configuration_rejects_invalid_vcpu_count(self):
        profile = json.loads(self.settings["managedProfile"])
        profile["cpu"]["vcpu_count"] = 3
        with self.assertRaisesRegex(ManagedSessionConfigError, "power of two"):
            managed_configuration_from_settings({**self.settings, "managedProfile": json.dumps(profile)})

    def test_configuration_requires_a_gpu_creation_policy(self):
        profile = json.loads(self.settings["managedProfile"])
        profile.pop("gpu")
        with self.assertRaisesRegex(ManagedSessionConfigError, "image and pool_ids"):
            managed_configuration_from_settings({**self.settings, "managedProfile": json.dumps(profile)})

    def test_default_state_root_is_plugin_local(self):
        self.assertEqual(default_state_root().name, ".runonrunpod")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
