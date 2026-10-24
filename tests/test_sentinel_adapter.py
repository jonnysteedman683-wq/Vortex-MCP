"""Sentinel adapter against stub Sentinel and Firebase token endpoints."""
import asyncio
import json
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from mcp.server.auth.provider import AccessToken

from vortex.adapters.sentinel import FirebaseIdTokens, SentinelClient, build_agent
from vortex.sdk import VortexAgent, VortexError, make_goal
from vortex.server import create_server
from vortex.settings import Settings
from vortex.store import VortexStore

AGENTS = ("hermes-1", "sentinel-1", "claude-operator")
OP = "claude-operator"


class Stub(BaseHTTPRequestHandler):
    calls: list = []
    exchanges = 0

    def log_message(self, *args):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        Stub.calls.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path == "/api/health":
            return self._reply(200, {"status": "ok"})
        if self.path == "/api/system/health-history":
            if self.headers.get("Authorization") != "Bearer id-token-1":
                return self._reply(401, {"error": "Unauthorized"})
            return self._reply(200, {"history": [1, 2]})
        return self._reply(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path.startswith("/token"):
            Stub.exchanges += 1
            form = urllib.parse.parse_qs(raw)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if form.get("refresh_token") == ["refresh-1"] and query.get("key") == ["api-key"]:
                return self._reply(200, {"id_token": "id-token-1", "expires_in": "3600", "refresh_token": "refresh-1"})
            return self._reply(400, {"error": "INVALID_REFRESH_TOKEN"})
        Stub.calls.append(("POST", self.path, self.headers.get("Authorization"), json.loads(raw or "{}")))
        if self.headers.get("Authorization") != "Bearer id-token-1":
            return self._reply(401, {"error": "Unauthorized"})
        return self._reply(200, {"success": True})


def _access(identity):
    return AccessToken(token="t", client_id=identity, subject=identity,
                       scopes=["vortex:connect", "vortex:read", "vortex:message"],
                       resource="http://127.0.0.1:8000/mcp")


class SentinelAdapterTests(unittest.TestCase):
    def setUp(self):
        Stub.calls, Stub.exchanges = [], 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.store = VortexStore(":memory:", agents={a: a for a in AGENTS})
        self.addCleanup(self.store.close)
        self.server = create_server(self.store, Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=(OP,),
            development_tokens={a: f"token-{a}" for a in AGENTS},
        ))

    def make_agent(self, tokens=None, allow_healing=False):
        async def call(tool, args):
            with patch("vortex.server.get_access_token", return_value=_access("sentinel-1")):
                try:
                    return await self.server._tool_manager.call_tool(tool, args)
                except Exception as exc:
                    raise VortexError(str(exc)) from exc
        return build_agent(VortexAgent(caller=call), SentinelClient(self.base, tokens), allow_healing)

    def dispatch(self, agent, action, args=None):
        task = self.store.create_task("hermes-1", make_goal(action, args), assigned_to="sentinel-1")
        asyncio.run(agent.run_once())
        return self.store.operator_get_task(OP, task["task_id"])

    def tokens(self, refresh="refresh-1"):
        return FirebaseIdTokens("api-key", refresh, token_url=f"{self.base}/token")

    def test_public_action_needs_no_credentials(self):
        task = self.dispatch(self.make_agent(), "health")
        self.assertEqual(json.loads(task["result"]), {"status": "ok"})
        self.assertIsNone(Stub.calls[-1][2])

    def test_signed_in_action_without_credentials_is_blocked(self):
        task = self.dispatch(self.make_agent(), "health_history")
        self.assertEqual(task["status"], "blocked")
        self.assertIn("needs a signed-in Sentinel user", task["blocked_reason"])

    def test_signed_in_action_exchanges_and_caches_id_token(self):
        agent = self.make_agent(self.tokens())
        self.assertEqual(json.loads(self.dispatch(agent, "health_history")["result"]), {"history": [1, 2]})
        self.dispatch(agent, "health_history")
        self.assertEqual(Stub.exchanges, 1)

    def test_bad_refresh_token_blocks_with_reason(self):
        task = self.dispatch(self.make_agent(self.tokens("revoked")), "health_history")
        self.assertIn("INVALID_REFRESH_TOKEN", task["blocked_reason"])

    def test_execute_healing_is_opt_in(self):
        refused = self.dispatch(self.make_agent(self.tokens()), "execute_healing", {"action_type": "FLUSH_LOGS"})
        self.assertIn("disabled", refused["blocked_reason"])
        self.assertFalse(any(c[0] == "POST" for c in Stub.calls))
        ok = self.dispatch(self.make_agent(self.tokens(), allow_healing=True), "execute_healing",
                           {"action_type": "FLUSH_LOGS"})
        self.assertEqual(ok["status"], "completed")
        self.assertEqual(Stub.calls[-1][3], {"actionType": "FLUSH_LOGS"})

    def test_token_exchange_requires_both_values(self):
        with self.assertRaises(ValueError):
            FirebaseIdTokens("", "refresh")


if __name__ == "__main__":
    unittest.main()
