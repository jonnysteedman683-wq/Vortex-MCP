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
            with (
                self.subTest(values=values),
                patch.dict(os.environ, values, clear=True),
                self.assertRaises(RuntimeError),
            ):
                Settings.from_env()

    def test_dev_server_cannot_bind_public_interface(self):
        with (
            patch.dict(os.environ, self.environment(VORTEX_HOST="0.0.0.0"), clear=True),
            self.assertRaises(RuntimeError),
        ):
            Settings.from_env()

    def test_oauth_mode_fails_closed_until_provider_setup_is_completed(self):
        with (
            patch.dict(os.environ, self.environment(VORTEX_AUTH_MODE="oauth"), clear=True),
            self.assertRaises(RuntimeError),
        ):
            Settings.from_env()

    def test_custom_agent_ids_are_supported(self):
        env = self.environment(
            VORTEX_AGENT_IDS="hermes-1,hermes-2,antigravity-1,spark-1,atlas-1",
            VORTEX_TOKEN_ATLAS_1="dev-token-5",
            VORTEX_SUBJECT_ATLAS_1="atlas-1",
            VORTEX_AUTH_MODE="oauth",
            VORTEX_PUBLIC_URL="https://vortex.example.test",
            VORTEX_OAUTH_ISSUER="https://id.example.test/realms/vortex",
            VORTEX_OAUTH_INTROSPECTION_URL="https://id.example.test/introspect",
            VORTEX_OAUTH_CLIENT_ID="introspector",
            VORTEX_OAUTH_CLIENT_SECRET="secret",
            VORTEX_SUBJECT_HERMES_1="hermes-1",
            VORTEX_SUBJECT_HERMES_2="hermes-2",
            VORTEX_SUBJECT_ANTIGRAVITY_1="antigravity-1",
            VORTEX_SUBJECT_SPARK_1="spark-1",
            VORTEX_ALLOWED_HOSTS="vortex.example.test",
            VORTEX_ALLOWED_ORIGINS="https://vortex.example.test",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.agent_ids, ("hermes-1", "hermes-2", "antigravity-1", "spark-1", "atlas-1"))
        self.assertEqual(settings.identity_clients["atlas-1"], "atlas-1")

    def test_oauth_mode_accepts_client_name_aliases(self):
        env = self.environment(
            VORTEX_AUTH_MODE="oauth",
            VORTEX_PUBLIC_URL="https://vortex.example.test",
            VORTEX_OAUTH_ISSUER="https://id.example.test/realms/vortex",
            VORTEX_OAUTH_INTROSPECTION_URL="https://id.example.test/introspect",
            VORTEX_OAUTH_CLIENT_ID="introspector",
            VORTEX_OAUTH_CLIENT_SECRET="secret",
            VORTEX_CLIENT_HERMES_1="hermes-1",
            VORTEX_CLIENT_HERMES_2="hermes-2",
            VORTEX_CLIENT_ANTIGRAVITY_1="antigravity-1",
            VORTEX_CLIENT_SPARK_1="spark-1",
            VORTEX_ALLOWED_HOSTS="vortex.example.test",
            VORTEX_ALLOWED_ORIGINS="https://vortex.example.test",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.agent_client_ids["hermes-1"], "hermes-1")
        self.assertEqual(settings.identity_clients["hermes-1"], "hermes-1")

    def test_ca_bundle_must_exist_when_set(self):
        env = self.environment(
            VORTEX_AUTH_MODE="oauth",
            VORTEX_PUBLIC_URL="https://vortex.example.test",
            VORTEX_OAUTH_ISSUER="https://id.example.test/realms/vortex",
            VORTEX_OAUTH_INTROSPECTION_URL="https://id.example.test/introspect",
            VORTEX_OAUTH_CLIENT_ID="introspector",
            VORTEX_OAUTH_CLIENT_SECRET="secret",
            VORTEX_SUBJECT_HERMES_1="hermes-1",
            VORTEX_SUBJECT_HERMES_2="hermes-2",
            VORTEX_SUBJECT_ANTIGRAVITY_1="antigravity-1",
            VORTEX_SUBJECT_SPARK_1="spark-1",
            VORTEX_ALLOWED_HOSTS="vortex.example.test",
            VORTEX_ALLOWED_ORIGINS="https://vortex.example.test",
            VORTEX_OAUTH_CA_BUNDLE="/does/not/exist.pem",
        )
        with (
            patch.dict(os.environ, env, clear=True),
            self.assertRaisesRegex(RuntimeError, "existing file"),
        ):
            Settings.from_env()

    def test_oauth_mode_parses_all_required_fields(self):
        env = self.environment(
            VORTEX_AUTH_MODE="oauth",
            VORTEX_PUBLIC_URL="https://vortex.example.test",
            VORTEX_OAUTH_ISSUER="https://id.example.test/realms/vortex",
            VORTEX_OAUTH_INTROSPECTION_URL="https://id.example.test/introspect",
            VORTEX_OAUTH_CLIENT_ID="introspector",
            VORTEX_OAUTH_CLIENT_SECRET="secret",
            VORTEX_SUBJECT_HERMES_1="hermes-1",
            VORTEX_SUBJECT_HERMES_2="hermes-2",
            VORTEX_SUBJECT_ANTIGRAVITY_1="antigravity-1",
            VORTEX_SUBJECT_SPARK_1="spark-1",
            VORTEX_ALLOWED_HOSTS="vortex.example.test, alt.example.test",
            VORTEX_ALLOWED_ORIGINS="https://vortex.example.test",
        )
        with patch.dict(os.environ, env, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.auth_mode, "oauth")
        self.assertEqual(settings.public_url, "https://vortex.example.test")
        self.assertEqual(settings.identity_clients["hermes-1"], "hermes-1")
        self.assertEqual(settings.allowed_hosts, ("vortex.example.test", "alt.example.test"))
        self.assertIsNone(settings.oauth_ca_bundle)

    def test_invalid_mode_and_out_of_range_port_are_rejected(self):
        for values in (self.environment(VORTEX_AUTH_MODE="unknown"),
                       self.environment(VORTEX_PORT="70000")):
            with (
                self.subTest(values=values),
                patch.dict(os.environ, values, clear=True),
                self.assertRaises(RuntimeError),
            ):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()
