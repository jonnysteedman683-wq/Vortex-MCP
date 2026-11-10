"""Tests proving local MCP auth is loopback-bound and OAuth mode uses provider IDs."""
import unittest

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings

from tests.backends import make_store
from vortex.server import DevelopmentTokenVerifier, create_server
from vortex.settings import Settings


class ServerAuthTests(unittest.TestCase):
    def test_dev_auth_maps_unique_tokens_to_stable_instance_id(self):
        verifier = DevelopmentTokenVerifier({
            "hermes-1": "token-one", "hermes-2": "token-two",
            "antigravity-1": "token-three", "spark-1": "token-four",
        }, "http://127.0.0.1:8000/mcp")
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
        store = make_store(":memory:")
        server = create_server(store, settings)
        self.assertIsInstance(server.settings.auth, AuthSettings)
        self.assertTrue(server.settings.auth.validate_token_resource)
        self.assertFalse(server.settings.auth.client_registration_options.enabled)
        self.assertIsInstance(server.settings.transport_security, TransportSecuritySettings)
        self.assertTrue(server.settings.transport_security.enable_dns_rebinding_protection)
        store.close()

    def test_development_server_exposes_durable_work_coordination_tools(self):
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8000,
            data_dir=".vortex-data", development_tokens={
                "hermes-1": "a", "hermes-2": "b", "antigravity-1": "c", "spark-1": "d",
            },
        )
        store = make_store(":memory:")
        server = create_server(store, settings)
        tool_names = {tool.name for tool in server._tool_manager.list_tools()}
        self.assertTrue({
            "vortex_create_task",
            "vortex_list_tasks",
            "vortex_get_task",
            "vortex_get_task_events",
            "vortex_claim_task",
            "vortex_update_task",
            "vortex_complete_task",
            "vortex_cancel_task",
            "vortex_spine_survey",
            "vortex_spine_plan",
        }.issubset(tool_names))
        store.close()

    def test_development_server_uses_configured_port_for_transport_and_token_resource(self):
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8123,
            data_dir=".vortex-data", development_tokens={
                "hermes-1": "a", "hermes-2": "b", "antigravity-1": "c", "spark-1": "d",
            },
        )
        store = make_store(":memory:")
        server = create_server(store, settings)
        security = server.settings.transport_security
        self.assertIn("127.0.0.1:8123", security.allowed_hosts)
        self.assertIn("http://localhost:8123", security.allowed_origins)

        import asyncio
        access = asyncio.run(server._token_verifier.verify_token("a"))
        self.assertEqual(access.resource, "http://127.0.0.1:8123/mcp")
        store.close()

    def test_development_token_verifier_rejects_non_loopback_resource_urls(self):
        with self.assertRaises(ValueError):
            DevelopmentTokenVerifier({"hermes-1": "token-a"}, "https://example.com/mcp")

    def test_development_server_uses_ipv6_loopback_resource_for_ipv6_bind(self):
        settings = Settings(
            auth_mode="development", host="::1", port=8124,
            data_dir=".vortex-data", development_tokens={
                "hermes-1": "a", "hermes-2": "b", "antigravity-1": "c", "spark-1": "d",
            },
        )
        store = make_store(":memory:")
        server = create_server(store, settings)
        self.assertEqual(server._token_verifier.resource_url, "http://[::1]:8124/mcp")
        self.assertIn("http://[::1]:8124", server.settings.transport_security.allowed_origins)
        store.close()


if __name__ == "__main__":
    unittest.main()
