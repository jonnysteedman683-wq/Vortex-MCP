"""OAuth token introspection and identity binding tests."""
import asyncio
import unittest

import httpx

from vortex.auth import IntrospectionTokenVerifier


class IntrospectionVerifierTests(unittest.TestCase):
    def make_verifier(self, claims, **overrides):
        def handler(request):
            self.assertEqual(request.url.path, "/oauth2/introspect")
            self.assertEqual(request.headers.get("authorization"), "Basic dGVzdC1jbGllbnQ6dGVzdC1zZWNyZXQ=")
            self.assertEqual(request.content.decode(), "token=presented-token&token_type_hint=access_token")
            return httpx.Response(200, json=claims)

        values = dict(
            introspection_url="https://id.example.test/oauth2/introspect",
            client_id="test-client",
            client_secret="test-secret",
            issuer="https://id.example.test/",
            resource_url="https://vortex.example.test/mcp",
            identity_clients={
                "hermes-1": "client-h1",
                "hermes-2": "client-h2",
                "antigravity-1": "client-ag",
                "spark-1": "client-spark",
            },
            transport=httpx.MockTransport(handler),
        )
        values.update(overrides)
        return IntrospectionTokenVerifier(**values)

    def test_active_correctly_scoped_token_maps_stable_subject_and_audience(self):
        verifier = self.make_verifier({
            "active": True,
            "sub": "user-h2",
            "iss": "https://id.example.test/",
            "aud": ["https://vortex.example.test/mcp"],
            "scope": "vortex:connect vortex:read vortex:message",
            "exp": 2000000000,
            "client_id": "client-h2",
        })
        token = asyncio.run(verifier.verify_token("presented-token"))
        self.assertEqual(token.client_id, "hermes-2")
        self.assertEqual(token.subject, "user-h2")
        self.assertEqual(token.resource, "https://vortex.example.test/mcp")
        self.assertIn("vortex:message", token.scopes)

    def test_inactive_token_is_rejected(self):
        verifier = self.make_verifier({"active": False})
        self.assertIsNone(asyncio.run(verifier.verify_token("presented-token")))

    def test_unknown_subject_is_rejected(self):
        verifier = self.make_verifier({
            "active": True, "sub": "rogue", "client_id": "rogue-client", "iss": "https://id.example.test/",
            "aud": "https://vortex.example.test/mcp", "scope": "vortex:connect",
        })
        self.assertIsNone(asyncio.run(verifier.verify_token("presented-token")))

    def test_wrong_issuer_and_audience_are_rejected(self):
        for claim, value in (("iss", "https://evil.example.test/"),
                             ("aud", "https://other.example.test/mcp")):
            claims = {
                "active": True, "sub": "user-h1", "client_id": "client-h1", "iss": "https://id.example.test/",
                "aud": "https://vortex.example.test/mcp", "scope": "vortex:connect",
            }
            claims[claim] = value
            with self.subTest(claim=claim):
                self.assertIsNone(asyncio.run(self.make_verifier(claims).verify_token("presented-token")))

    def test_expired_or_scope_less_token_is_rejected(self):
        base = {
            "active": True, "sub": "user-h1", "client_id": "client-h1", "iss": "https://id.example.test/",
            "aud": "https://vortex.example.test/mcp", "scope": "vortex:connect",
        }
        for changes in ({"exp": 1}, {"scope": "openid profile"}):
            claims = base | changes
            with self.subTest(changes=changes):
                self.assertIsNone(asyncio.run(self.make_verifier(claims).verify_token("presented-token")))

    def test_non_https_provider_endpoints_and_ambiguous_subjects_are_rejected(self):
        with self.assertRaises(ValueError):
            self.make_verifier({}, introspection_url="http://id.example.test/introspect")
        with self.assertRaises(ValueError):
            self.make_verifier({}, identity_clients={
                "hermes-1": "same", "hermes-2": "same",
                "antigravity-1": "ag", "spark-1": "spark",
            })

    def test_provider_errors_fail_closed(self):
        transport = httpx.MockTransport(lambda request: httpx.Response(503))
        verifier = self.make_verifier({}, transport=transport)
        self.assertIsNone(asyncio.run(verifier.verify_token("presented-token")))

    def test_malformed_issuer_audience_and_expiry_fail_closed(self):
        base = {
            "active": True, "sub": "user-h1", "client_id": "client-h1",
            "iss": "https://id.example.test/", "aud": "https://vortex.example.test/mcp",
            "scope": "vortex:connect vortex:read vortex:message", "exp": 2000000000,
        }
        for changes in (
            {"iss": 123},
            {"aud": ["https://vortex.example.test/mcp", None]},
            {"exp": True},
            {"exp": float("nan")},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(asyncio.run(self.make_verifier(base | changes).verify_token("presented-token")))


if __name__ == "__main__":
    unittest.main()
