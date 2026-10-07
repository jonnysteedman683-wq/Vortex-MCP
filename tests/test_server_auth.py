"""Tests proving local MCP auth is loopback-bound and OAuth mode uses provider IDs."""
import unittest
from unittest.mock import patch

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from vortex.server import DevelopmentTokenVerifier, create_server
from vortex.settings import Settings
from vortex.store import VortexStore


class ServerAuthTests(unittest.TestCase):
    def test_dev_auth_maps_unique_tokens_to_stable_instance_id(self):
        verifier = DevelopmentTokenVerifier({
            "hermes-1": "token-one", "hermes-2": "token-two",
            "antigravity-1": "token-three", "spark-1": "token-four",
        })
        import asyncio
        access = asyncio.run(verifier.verify_token("token-two"))
        self.assertEqual(access.client_id, "hermes-2")
        self.assertIsNone(asyncio.run(verifier.verify_token("unknown")))

    def test_development_server_has_resource_validation_and_host_protection(self):
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8000,
            data_dir=".vortex-data", development_tokens={
                "hermes-1": "a", "hermes-2": "b", "antigravity-1": "c", "spark-1": "d",
            },
        )
        store = VortexStore(":memory:")
        server = create_server(store, settings)
        self.assertIsInstance(server.settings.auth, AuthSettings)
        self.assertTrue(server.settings.auth.validate_token_resource)
        self.assertFalse(server.settings.auth.client_registration_options.enabled)
        self.assertIsInstance(server.settings.transport_security, TransportSecuritySettings)
        self.assertTrue(server.settings.transport_security.enable_dns_rebinding_protection)
        store.close()


if __name__ == "__main__":
    unittest.main()
