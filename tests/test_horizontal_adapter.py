"""HORIZONTAL adapter against a stub Sovereign gateway and an in-process Vortex."""
import asyncio
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar
from unittest.mock import patch

from mcp.server.auth.provider import AccessToken

from vortex.adapters.horizontal import (
    DEFAULT_COMMANDS,
    GatewayError,
    HorizontalGateway,
    allowed_commands,
    build_agent,
)
from vortex.sdk import VortexAgent, VortexError, make_goal
from vortex.server import create_server
from vortex.settings import Settings
from vortex.store import VortexStore

AGENTS = ("hermes-1", "horizontal-1", "claude-operator")
OP = "claude-operator"
GATEWAY_TOKEN = "citadel-test-token"


class StubGateway(BaseHTTPRequestHandler):
    calls: ClassVar[list] = []

    def log_message(self, *args):
        pass

    def _reply(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        return self.headers.get("Authorization") == f"Bearer {GATEWAY_TOKEN}"

    def do_GET(self):
        StubGateway.calls.append(("GET", self.path, None))
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        if self.path == "/api/status":
            return self._reply(200, {"status": "OPERATIONAL", "total_repos": 3})
        if self.path.startswith("/api/search?"):
            return self._reply(200, {"results": ["hit"], "path": self.path})
        return self._reply(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        StubGateway.calls.append(("POST", self.path, body))
        if not self._authorized():
            return self._reply(401, {"error": "unauthorized"})
        return self._reply(200, {"status": "success", "echo": body})


def _access(identity):
    return AccessToken(token="t", client_id=identity, subject=identity,
                       scopes=["vortex:connect", "vortex:read", "vortex:message"],
                       resource="http://127.0.0.1:8000/mcp")


class HorizontalAdapterTests(unittest.TestCase):
    def setUp(self):
        StubGateway.calls = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), StubGateway)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.gateway_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

        self.store = VortexStore(":memory:", agents={a: a for a in AGENTS})
        self.addCleanup(self.store.close)
        server = create_server(self.store, Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=(OP,),
            development_tokens={a: f"token-{a}" for a in AGENTS},
        ))

        async def call(tool, args):
            with patch("vortex.server.get_access_token", return_value=_access("horizontal-1")):
                try:
                    return await server._tool_manager.call_tool(tool, args)
                except Exception as exc:
                    raise VortexError(str(exc)) from exc

        self.agent = build_agent(
            VortexAgent(caller=call),
            HorizontalGateway(self.gateway_url, GATEWAY_TOKEN),
            allowed_commands({}),
        )

    def dispatch(self, action, args=None):
        task = self.store.create_task("hermes-1", make_goal(action, args), assigned_to="horizontal-1")
        asyncio.run(self.agent.run_once())
        return self.store.operator_get_task(OP, task["task_id"])

    def test_status_is_fetched_with_the_gateway_token(self):
        task = self.dispatch("status")
        self.assertEqual(task["status"], "completed")
        self.assertEqual(json.loads(task["result"])["status"], "OPERATIONAL")
        self.assertEqual(StubGateway.calls[-1][:2], ("GET", "/api/status"))

    def test_search_url_encodes_query(self):
        task = self.dispatch("search", {"q": "zero trust & more"})
        self.assertIn("q=zero+trust+%26+more", json.loads(task["result"])["path"])
        self.assertIn("non-empty 'q'", self.dispatch("search", {"q": " "})["blocked_reason"])

    def test_read_only_command_runs(self):
        task = self.dispatch("command", {"command": "scan", "target": "repo"})
        self.assertEqual(task["status"], "completed")
        self.assertEqual(StubGateway.calls[-1], ("POST", "/api/command", {"action": "scan", "target": "repo"}))

    def test_mutating_command_is_refused_unless_opted_in(self):
        task = self.dispatch("command", {"command": "advance_gen"})
        self.assertEqual(task["status"], "blocked")
        self.assertIn("not allowed by this adapter", task["blocked_reason"])
        self.assertFalse(any(c[0] == "POST" for c in StubGateway.calls))
        self.assertIn("advance_gen", allowed_commands({"HORIZONTAL_ADAPTER_EXTRA_COMMANDS": "advance_gen, harvest"}))
        self.assertTrue(DEFAULT_COMMANDS <= allowed_commands({}))

    def test_gateway_errors_block_the_task_with_detail(self):
        bad = HorizontalGateway(self.gateway_url, "wrong-token")
        with self.assertRaises(GatewayError) as caught:
            bad.request("GET", "/api/status")
        self.assertIn("returned 401", str(caught.exception))
        with self.assertRaises(GatewayError) as caught:
            HorizontalGateway("http://127.0.0.1:9", GATEWAY_TOKEN, timeout=2).request("GET", "/api/status")
        self.assertIn("unreachable", str(caught.exception))
        with self.assertRaises(ValueError):
            HorizontalGateway(self.gateway_url, "")


if __name__ == "__main__":
    unittest.main()
