"""Tests proving local MCP auth is loopback-bound and OAuth mode uses provider IDs."""
import unittest

from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from starlette.testclient import TestClient

from tests.backends import make_store
from vortex.keys import ApiKeyTokenVerifier
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


PUBLIC_URL = "https://vortex.example.test"


def _apikey_settings(**overrides):
    values = {
        "auth_mode": "apikey", "host": "127.0.0.1", "port": 8000, "data_dir": ".vortex-data",
        "agent_ids": ("hermes-1", "hermes-2", "antigravity-1", "spark-1"),
        "operator_ids": ("hermes-2",),
        "public_url": PUBLIC_URL,
        "allowed_hosts": ("vortex.example.test",),
        "allowed_origins": (PUBLIC_URL,),
    }
    values.update(overrides)
    return Settings(**values)


class ApiKeyServerTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(":memory:")
        self.addCleanup(self.store.close)
        self.server = create_server(self.store, _apikey_settings())

    def test_apikey_mode_uses_the_key_verifier_and_the_public_url(self):
        server = self.server
        self.assertIsInstance(server._token_verifier, ApiKeyTokenVerifier)
        self.assertEqual(server._token_verifier.resource_url, PUBLIC_URL + "/mcp")
        auth = server.settings.auth
        self.assertEqual(str(auth.issuer_url).rstrip("/"), PUBLIC_URL)
        self.assertEqual(str(auth.resource_server_url), PUBLIC_URL + "/mcp")
        self.assertTrue(auth.validate_token_resource)
        self.assertFalse(auth.client_registration_options.enabled)
        self.assertEqual(auth.required_scopes, ["vortex:connect", "vortex:read", "vortex:message"])
        security = server.settings.transport_security
        self.assertTrue(security.enable_dns_rebinding_protection)
        self.assertEqual(security.allowed_hosts, ["vortex.example.test"])
        self.assertEqual(security.allowed_origins, [PUBLIC_URL])

    def test_apikey_mode_refuses_to_open_a_sqlite_store(self):
        with self.assertRaisesRegex(RuntimeError, "Postgres"):
            create_server(None, _apikey_settings(data_dir="/nonexistent/vortex-apikey-test"))

    def test_issued_key_authenticates_as_its_agent(self):
        import asyncio

        key = self.store.issue_key("hermes-1")
        access = asyncio.run(self.server._token_verifier.verify_token(key))
        self.assertEqual(access.client_id, "hermes-1")
        self.assertEqual(access.resource, PUBLIC_URL + "/mcp")
        self.assertIsNone(asyncio.run(self.server._token_verifier.verify_token("a")))

    def test_http_requests_are_authenticated_by_key(self):
        operator_key = self.store.issue_key("hermes-2")
        worker_key = self.store.issue_key("hermes-1")
        near_miss = operator_key[:-1] + ("A" if operator_key[-1] != "A" else "B")
        with TestClient(self.server.streamable_http_app(), base_url=PUBLIC_URL) as client:
            def get(token=None):
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                return client.get("/events", params={"once": 1}, headers=headers)

            self.assertEqual(get().status_code, 401)
            self.assertEqual(get("not-a-key").status_code, 401)
            self.assertEqual(get(near_miss).status_code, 401)
            self.assertEqual(get(worker_key).status_code, 403)  # a valid agent, but not an operator
            self.assertEqual(get(operator_key).status_code, 200)
            for response in (get("not-a-key"), get(near_miss)):
                self.assertNotIn("vtx_", response.text)

    def test_revoked_key_is_refused_over_http(self):
        key = self.store.issue_key("hermes-2")
        self.store.revoke_keys("hermes-2")
        with TestClient(self.server.streamable_http_app(), base_url=PUBLIC_URL) as client:
            response = client.get("/events", params={"once": 1}, headers={"Authorization": f"Bearer {key}"})
        self.assertEqual(response.status_code, 401)

    def test_key_for_an_agent_this_server_does_not_configure_is_not_an_identity(self):
        server = create_server(self.store, _apikey_settings(agent_ids=("hermes-1",), operator_ids=("hermes-1",)))
        key = self.store.issue_key("hermes-2")
        with TestClient(server.streamable_http_app(), base_url=PUBLIC_URL) as client:
            response = client.get("/events", params={"once": 1}, headers={"Authorization": f"Bearer {key}"})
        self.assertIn(response.status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
