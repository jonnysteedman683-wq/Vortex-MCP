"""Fail-closed configuration validation tests."""
import os
import unittest
from unittest.mock import patch

from vortex.settings import AGENT_IDS, Settings


class SettingsTests(unittest.TestCase):
    def environment(self, **overrides):
        values = {
            "VORTEX_AUTH_MODE": "development",
            "VORTEX_HOST": "127.0.0.1",
            "VORTEX_PORT": "8000",
            "VORTEX_DATA_DIR": ".vortex-data",
            "VORTEX_TOKEN_HERMES_1": "dev-token-1",
            "VORTEX_TOKEN_HERMES_2": "dev-token-2",
            "VORTEX_TOKEN_ANTIGRAVITY_1": "dev-token-3",
            "VORTEX_TOKEN_SPARK_1": "dev-token-4",
        }
        values.update(overrides)
        return values

    def test_valid_local_dev_requires_distinct_tokens_and_loopback(self):
        with patch.dict(os.environ, self.environment(), clear=True):
            settings = Settings.from_env()
        self.assertEqual(set(settings.development_tokens), set(AGENT_IDS))
        self.assertEqual(settings.host, "127.0.0.1")

    def test_duplicate_or_missing_local_token_is_rejected(self):
        for values in (self.environment(VORTEX_TOKEN_SPARK_1="dev-token-1"),
                       {k: v for k, v in self.environment().items() if k != "VORTEX_TOKEN_HERMES_2"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(RuntimeError):
                    Settings.from_env()

    def test_dev_server_cannot_bind_public_interface(self):
        with patch.dict(os.environ, self.environment(VORTEX_HOST="0.0.0.0"), clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_oauth_mode_fails_closed_until_provider_setup_is_completed(self):
        with patch.dict(os.environ, self.environment(VORTEX_AUTH_MODE="oauth"), clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_env()

    def test_invalid_mode_and_out_of_range_port_are_rejected(self):
        for values in (self.environment(VORTEX_AUTH_MODE="unknown"),
                       self.environment(VORTEX_PORT="70000")):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(RuntimeError):
                    Settings.from_env()


if __name__ == "__main__":
    unittest.main()
