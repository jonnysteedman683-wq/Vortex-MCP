"""Tests for the kill switch, the /events stream, the adapter SDK and the watcher."""
import asyncio
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from tests.backends import make_store, sqlite_only
from vortex.sdk import VortexAgent, VortexError, make_goal, parse_goal
from vortex.server import create_server
from vortex.settings import Settings
from vortex.watcher import WatchState, compute_alerts, notify, poll_once

AGENTS = ("hermes-1", "worker-1", "worker-2", "claude-operator")
OP = "claude-operator"


def _store(path=":memory:", policy=None):
    return make_store(path, agents={a: a for a in AGENTS}, approval_required_for=policy)


def _server(store, policy=()):
    settings = Settings(
        auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
        agent_ids=AGENTS, operator_ids=(OP,), approval_required_for=tuple(policy),
        development_tokens={a: f"token-{a}" for a in AGENTS},
    )
    return create_server(store, settings)


def _access(identity):
    return AccessToken(token="t", client_id=identity, subject=identity,
                       scopes=["vortex:connect", "vortex:read", "vortex:message"],
                       resource="http://127.0.0.1:8000/mcp")


def in_process_caller(server, identity):
    """SDK/watcher transport that calls the real server tools as `identity`."""
    async def call(tool, args):
        with patch("vortex.server.get_access_token", return_value=_access(identity)):
            try:
                return await server._tool_manager.call_tool(tool, args)
            except Exception as exc:
                raise VortexError(str(exc)) from exc
    return call


class KillSwitchStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()
        self.addCleanup(self.store.close)

    def test_paused_agent_cannot_claim_renew_or_complete(self):
        a = self.store.create_task("hermes-1", "a", assigned_to="worker-1")
        b = self.store.create_task("hermes-1", "b", assigned_to="worker-1")
        self.store.claim_task("worker-1", a["task_id"])
        self.store.operator_pause_agent(OP, "worker-1", "runaway loop")
        for action in (
            lambda: self.store.claim_task("worker-1", b["task_id"]),
            lambda: self.store.update_task("worker-1", a["task_id"], "in_progress"),
            lambda: self.store.complete_task("worker-1", a["task_id"], result="x"),
        ):
            with self.assertRaises(PermissionError) as caught:
                action()
            self.assertIn("paused by an operator: runaway loop", str(caught.exception))
        self.assertEqual(self.store.operator_overview(OP)["paused_agents"], ["worker-1"])
        self.store.operator_resume_agent(OP, "worker-1")
        self.store.claim_task("worker-1", b["task_id"])
        self.assertEqual(self.store.operator_overview(OP)["paused_agents"], [])

    def test_pause_with_cancel_active_cancels_and_audits(self):
        a = self.store.create_task("hermes-1", "a", assigned_to="worker-1")
        b = self.store.create_task("hermes-1", "b", assigned_to="worker-1")
        untouched = self.store.create_task("hermes-1", "c", assigned_to="worker-2")
        self.store.claim_task("worker-1", a["task_id"])
        self.store.claim_task("worker-1", b["task_id"])
        self.store.update_task("worker-1", b["task_id"], "blocked", blocked_reason="waiting")
        result = self.store.operator_pause_agent(OP, "worker-1", "kill", cancel_active=True)
        self.assertEqual(sorted(result["cancelled_tasks"]), sorted([a["task_id"], b["task_id"]]))
        for task_id in (a["task_id"], b["task_id"]):
            task = self.store.operator_get_task(OP, task_id)
            self.assertEqual(task["status"], "cancelled")
            self.assertEqual(task["events"][-1]["details"]["reason"], "agent paused: kill")
        self.assertEqual(self.store.operator_get_task(OP, untouched["task_id"])["status"], "open")
        log = self.store.operator_audit_log(OP)
        self.assertEqual(log[0]["action"], "pause_agent")
        self.assertEqual(log[0]["target_id"], "worker-1")
        self.assertTrue(log[0]["details"]["cancel_active"])

    def test_pause_validation(self):
        with self.assertRaises(ValueError):
            self.store.operator_pause_agent(OP, "worker-1", "  ")
        with self.assertRaises(ValueError):
            self.store.operator_pause_agent(OP, OP, "self")
        with self.assertRaises(ValueError):
            self.store.operator_pause_agent(OP, "ghost", "x")
        with self.assertRaises(ValueError):
            self.store.operator_resume_agent(OP, "worker-1")

    @sqlite_only
    def test_old_database_gains_pause_columns(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript("""
                CREATE TABLE agents (agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'offline',
                    last_seen TEXT, created_at TEXT NOT NULL);
                INSERT INTO agents VALUES ('worker-1','w','spark','offline',NULL,'2026-01-01T00:00:00+00:00');
            """)
            conn.commit()
            conn.close()
            store = _store(path)
            try:
                store.operator_pause_agent(OP, "worker-1", "migrated")
                self.assertEqual(store.operator_overview(OP)["paused_agents"], ["worker-1"])
            finally:
                store.close()


class KillSwitchServerTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()
        self.addCleanup(self.store.close)
        self.server = _server(self.store)

    def test_non_operator_cannot_use_kill_switch(self):
        for name, args in [
            ("vortex_operator_pause_agent", {"agent_id": "worker-1", "reason": "x"}),
            ("vortex_operator_resume_agent", {"agent_id": "worker-1"}),
            ("vortex_operator_audit_log", {}),
        ]:
            with self.subTest(tool=name):
                with self.assertRaises(VortexError) as caught:
                    asyncio.run(in_process_caller(self.server, "worker-2")(name, args))
                self.assertIn("Operator privileges required", str(caught.exception))
        self.assertEqual(self.store.operator_overview(OP)["paused_agents"], [])


class EventStreamTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()
        self.addCleanup(self.store.close)
        self.client = TestClient(_server(self.store).streamable_http_app())
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _get(self, token=None, **params):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return self.client.get("/events", params=params, headers=headers)

    def test_requires_operator_token(self):
        self.assertEqual(self._get().status_code, 401)
        self.assertEqual(self._get("not-a-token", once=1).status_code, 401)
        self.assertEqual(self._get("token-worker-1", once=1).status_code, 403)

    def test_streams_events_and_resumes_after_cursor(self):
        task = self.store.create_task("hermes-1", "stream me", assigned_to="worker-1")
        self.store.claim_task("worker-1", task["task_id"])
        response = self._get(f"token-{OP}", once=1)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        frames = [f for f in response.text.split("\n\n") if f.startswith("id:")]
        events = [json.loads(f.split("data: ", 1)[1]) for f in frames]
        self.assertEqual([e["event_type"] for e in events], ["created", "claimed"])
        first_id = events[0]["event_id"]
        resumed = self._get(f"token-{OP}", once=1, after=first_id)
        self.assertIn('"event_type": "claimed"', resumed.text)
        self.assertNotIn('"event_type": "created"', resumed.text)
        self.assertEqual(self._get(f"token-{OP}", once=1, after=-1).status_code, 400)


class SdkTests(unittest.TestCase):
    def setUp(self):
        self.store = _store()
        self.addCleanup(self.store.close)
        self.server = _server(self.store)
        self.agent = VortexAgent(caller=in_process_caller(self.server, "worker-1"))
        self.calls = []

        @self.agent.action("echo")
        def echo(args):
            self.calls.append(args)
            return {"echo": args}

        @self.agent.action("boom")
        async def boom(args):
            raise RuntimeError("disk full")

    def run_once(self):
        return asyncio.run(self.agent.run_once())

    def test_goal_helpers(self):
        self.assertEqual(parse_goal(make_goal("x", {"a": 1})), ("x", {"a": 1}))
        self.assertEqual(parse_goal("plain text goal"), (None, {}))
        self.assertEqual(parse_goal('{"action": 5}'), (None, {}))

    def test_executes_actions_and_reports_failures(self):
        ok = self.store.create_task("hermes-1", make_goal("echo", {"n": 1}), assigned_to="worker-1")
        bad = self.store.create_task("hermes-1", make_goal("boom"), assigned_to="worker-1")
        unknown = self.store.create_task("hermes-1", make_goal("teleport"), assigned_to="worker-1")
        other = self.store.create_task("hermes-1", make_goal("echo"), assigned_to="worker-2")
        counts = self.run_once()
        self.assertEqual(counts, {"completed": 1, "blocked": 2, "skipped": 0})
        self.assertEqual(self.calls, [{"n": 1}])
        done = self.store.operator_get_task(OP, ok["task_id"])
        self.assertEqual(done["status"], "completed")
        self.assertEqual(json.loads(done["result"]), {"echo": {"n": 1}})
        self.assertIn("RuntimeError: disk full", self.store.operator_get_task(OP, bad["task_id"])["blocked_reason"])
        self.assertIn("Unsupported action 'teleport'", self.store.operator_get_task(OP, unknown["task_id"])["blocked_reason"])
        self.assertEqual(self.store.operator_get_task(OP, other["task_id"])["status"], "open")
        self.assertEqual(self.agent.agent_id, "worker-1")

    def test_respects_approval_gate_and_pause(self):
        gated = self.store.create_task("hermes-1", make_goal("echo"), assigned_to="worker-1",
                                       requires_approval=True)
        self.assertEqual(self.run_once()["completed"], 0)
        self.store.operator_approve_task(OP, gated["task_id"])
        self.store.operator_pause_agent(OP, "worker-1", "hold")
        self.assertEqual(self.run_once(), {"completed": 0, "blocked": 0, "skipped": 1})
        self.store.operator_resume_agent(OP, "worker-1")
        self.assertEqual(self.run_once()["completed"], 1)


class SdkOverHttpTests(unittest.TestCase):
    """The SDK's default transport against a real server, so errors cross the real MCP client."""

    def setUp(self):
        import socket
        import threading
        import time

        import uvicorn

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.store = _store()
        self.addCleanup(self.store.close)
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=self.port, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=(OP,),
            development_tokens={a: f"token-{a}" for a in AGENTS},
        )
        app = create_server(self.store, settings).streamable_http_app()
        self.uvicorn = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        thread = threading.Thread(target=self.uvicorn.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while not self.uvicorn.started and time.time() < deadline:
            time.sleep(0.05)
        self.addCleanup(thread.join, 5)
        self.addCleanup(setattr, self.uvicorn, "should_exit", True)
        self.url = f"http://127.0.0.1:{self.port}/mcp"

    def test_paused_agent_skips_instead_of_crashing(self):
        agent = VortexAgent(self.url, "token-worker-1")
        agent.action("echo")(lambda args: "ok")
        self.store.create_task("hermes-1", make_goal("echo"), assigned_to="worker-1")
        self.store.operator_pause_agent(OP, "worker-1", "hold")
        self.assertEqual(asyncio.run(agent.run_once()), {"completed": 0, "blocked": 0, "skipped": 1})
        self.store.operator_resume_agent(OP, "worker-1")
        self.assertEqual(asyncio.run(agent.run_once())["completed"], 1)

    def test_tool_errors_raise_vortex_error(self):
        agent = VortexAgent(self.url, "token-worker-1")
        with self.assertRaises(VortexError):
            asyncio.run(agent.call("vortex_operator_overview"))


class WatcherTests(unittest.TestCase):
    def overview(self, online=(), expired=0, paused=()):
        return {
            "agents": [{"agent_id": a, "status": "online" if a in online else "offline"} for a in AGENTS],
            "expired_leases": expired,
            "paused_agents": list(paused),
        }

    def test_first_poll_primes_except_pending_approvals(self):
        pending = [{"task_id": "t1", "goal": "deploy", "assigned_to": "worker-1"}]
        alerts, state = compute_alerts(WatchState(), self.overview(online=("worker-1",), expired=3),
                                       pending, [{"task_id": "b1", "blocked_reason": "x"}])
        self.assertEqual([a.kind for a in alerts], ["approval"])
        self.assertTrue(state.primed)

    def test_alerts_fire_once_per_change(self):
        _, state = compute_alerts(WatchState(), self.overview(online=("worker-1", "worker-2")), [], [])
        alerts, state = compute_alerts(
            state,
            self.overview(online=("worker-2",), expired=1, paused=("worker-1",)),
            [{"task_id": "t2", "goal": "rm -rf build", "assigned_to": None}],
            [{"task_id": "b2", "assigned_to": "worker-2", "blocked_reason": "no creds"}],
        )
        self.assertEqual(sorted(a.kind for a in alerts), ["approval", "blocked", "lease", "offline", "paused"])
        self.assertIn("worker-1 went offline", " ".join(a.message for a in alerts))
        repeat, _ = compute_alerts(
            state,
            self.overview(online=("worker-2",), expired=1, paused=("worker-1",)),
            [{"task_id": "t2", "goal": "rm -rf build", "assigned_to": None}],
            [{"task_id": "b2", "assigned_to": "worker-2", "blocked_reason": "no creds"}],
        )
        self.assertEqual(repeat, [])

    def test_poll_once_against_real_server(self):
        store = _store()
        self.addCleanup(store.close)
        server = _server(store)
        store.create_task("hermes-1", "needs a human", assigned_to="worker-1", requires_approval=True)
        alerts, state = asyncio.run(poll_once(in_process_caller(server, OP), WatchState()))
        self.assertEqual([a.kind for a in alerts], ["approval"])
        self.assertEqual(len(state.pending), 1)

    def test_notify_runs_command_without_shell(self):
        from vortex.watcher import Alert
        with TemporaryDirectory() as temp:
            out = Path(temp) / "alert.txt"
            script = f"import os,sys; open({str(out)!r},'w').write(os.environ['VORTEX_ALERT_TEXT'] + '|' + sys.stdin.read())"
            command = f"{sys.executable} -c {json.dumps(script)}"
            notify([Alert("approval", "Approval needed; $(touch pwned)", "k")],
                   env={"VORTEX_ALERT_COMMAND": command, "PATH": os.environ.get("PATH", "")})
            text = out.read_text()
            self.assertIn("[approval] Approval needed; $(touch pwned)", text)
            self.assertFalse((Path.cwd() / "pwned").exists())


if __name__ == "__main__":
    unittest.main()
