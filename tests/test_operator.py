"""Tests for operator oversight: fleet-wide read/cancel, gated by token identity."""
import asyncio
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mcp.server.auth.provider import AccessToken

from vortex.server import create_server
from vortex.settings import Settings
from vortex.store import VortexStore

AGENTS = ("hermes-1", "hermes-2", "spark-1", "claude-operator")


def _access(agent_id: str) -> AccessToken:
    return AccessToken(
        token="t", client_id=agent_id, subject=agent_id,
        scopes=["vortex:connect", "vortex:read", "vortex:message"],
        resource="http://127.0.0.1:8000/mcp",
    )


class OperatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = VortexStore(
            Path(self.temp.name) / "op.sqlite3", agents={agent: agent for agent in AGENTS}
        )
        self.addCleanup(self.store.close)

    def test_operator_sees_tasks_it_neither_created_nor_owns(self):
        task = self.store.create_task("hermes-1", "private work", assigned_to="hermes-2")
        with self.assertRaises(PermissionError):
            self.store.get_task("spark-1", task["task_id"])
        listed = self.store.operator_list_tasks("claude-operator")
        self.assertEqual([t["task_id"] for t in listed], [task["task_id"]])
        detail = self.store.operator_get_task("claude-operator", task["task_id"])
        self.assertEqual(detail["events"][0]["event_type"], "created")

    def test_operator_list_filters_by_status_and_assignee(self):
        a = self.store.create_task("hermes-1", "a", assigned_to="hermes-2")
        self.store.create_task("hermes-1", "b", assigned_to="spark-1")
        self.store.claim_task("hermes-2", a["task_id"])
        in_progress = self.store.operator_list_tasks("claude-operator", status="in_progress")
        self.assertEqual([t["task_id"] for t in in_progress], [a["task_id"]])
        for_spark = self.store.operator_list_tasks("claude-operator", assigned_to="spark-1")
        self.assertEqual([t["goal"] for t in for_spark], ["b"])
        with self.assertRaises(ValueError):
            self.store.operator_list_tasks("claude-operator", status="bogus")
        with self.assertRaises(ValueError):
            self.store.operator_list_tasks("claude-operator", assigned_to="nobody")

    def test_operator_cancel_requires_reason_and_is_audited(self):
        task = self.store.create_task("hermes-1", "runaway", assigned_to="hermes-2")
        self.store.claim_task("hermes-2", task["task_id"])
        with self.assertRaises(ValueError):
            self.store.operator_cancel_task("claude-operator", task["task_id"], "  ")
        cancelled = self.store.operator_cancel_task("claude-operator", task["task_id"], "kill switch drill")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["lease_expires_at"])
        last = self.store.operator_get_task("claude-operator", task["task_id"])["events"][-1]
        self.assertEqual(last["actor_id"], "claude-operator")
        self.assertEqual(last["details"], {
            "reason": "kill switch drill", "by_operator": True, "previous_status": "in_progress",
        })
        with self.assertRaises(ValueError):
            self.store.operator_cancel_task("claude-operator", task["task_id"], "again")

    def test_overview_counts_work_presence_and_unread(self):
        self.store.register("hermes-2")
        t = self.store.create_task("hermes-1", "x", assigned_to="hermes-2")
        self.store.claim_task("hermes-2", t["task_id"])
        self.store.create_task("hermes-1", "y", assigned_to="spark-1")
        self.store.send("hermes-1", "spark-1", "ping")
        overview = self.store.operator_overview("claude-operator")
        self.assertEqual(overview["task_counts"]["in_progress"], 1)
        self.assertEqual(overview["task_counts"]["open"], 1)
        self.assertEqual(overview["expired_leases"], 0)
        agents = {a["agent_id"]: a for a in overview["agents"]}
        self.assertEqual(agents["hermes-2"]["status"], "online")
        self.assertEqual(agents["hermes-2"]["active_tasks"], {"in_progress": 1})
        self.assertEqual(agents["spark-1"]["unread_messages"], 1)
        self.assertEqual(agents["claude-operator"]["platform"], "claude")

    def test_event_feed_pages_in_order(self):
        t = self.store.create_task("hermes-1", "feed", assigned_to="hermes-2")
        self.store.claim_task("hermes-2", t["task_id"])
        self.store.complete_task("hermes-2", t["task_id"], result="done")
        first = self.store.operator_events("claude-operator", limit=2)
        self.assertEqual([e["event_type"] for e in first], ["created", "claimed"])
        rest = self.store.operator_events("claude-operator", after_event_id=first[-1]["event_id"])
        self.assertEqual([e["event_type"] for e in rest], ["completed"])
        self.assertEqual(rest[0]["task_goal"], "feed")
        with self.assertRaises(ValueError):
            self.store.operator_events("claude-operator", after_event_id=-1)


class OperatorSettingsTests(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "VORTEX_AUTH_MODE": "development",
            "VORTEX_AGENT_IDS": ",".join(AGENTS),
            **{f"VORTEX_TOKEN_{a.upper().replace('-', '_')}": f"token-{a}" for a in AGENTS},
        }
        values.update(overrides)
        return values

    def test_operators_default_to_none(self):
        with patch.dict(os.environ, self.env(), clear=True):
            self.assertEqual(Settings.from_env().operator_ids, ())

    def test_operator_must_be_a_configured_agent(self):
        with patch.dict(os.environ, self.env(VORTEX_OPERATOR_IDS="claude-operator"), clear=True):
            self.assertEqual(Settings.from_env().operator_ids, ("claude-operator",))
        with patch.dict(os.environ, self.env(VORTEX_OPERATOR_IDS="intruder"), clear=True):
            with self.assertRaises(RuntimeError):
                Settings.from_env()
        with patch.dict(
            os.environ, self.env(VORTEX_OPERATOR_IDS="claude-operator,claude-operator"), clear=True
        ):
            with self.assertRaises(RuntimeError):
                Settings.from_env()


class OperatorServerTests(unittest.TestCase):
    def setUp(self):
        self.store = VortexStore(":memory:", agents={agent: agent for agent in AGENTS})
        self.addCleanup(self.store.close)
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=("claude-operator",),
            development_tokens={agent: f"token-{agent}" for agent in AGENTS},
        )
        self.server = create_server(self.store, settings)

    def call(self, identity, name, args=None):
        with patch("vortex.server.get_access_token", return_value=_access(identity)):
            return asyncio.run(self.server._tool_manager.call_tool(name, args or {}))

    def test_operator_tools_are_registered(self):
        names = {tool.name for tool in self.server._tool_manager.list_tools()}
        self.assertTrue({
            "vortex_operator_overview", "vortex_operator_list_tasks", "vortex_operator_get_task",
            "vortex_operator_cancel_task", "vortex_operator_events",
        }.issubset(names))

    def test_non_operator_token_is_refused_on_every_operator_tool(self):
        task = self.store.create_task("hermes-1", "guarded", assigned_to="hermes-2")
        calls = [
            ("vortex_operator_overview", {}),
            ("vortex_operator_list_tasks", {}),
            ("vortex_operator_get_task", {"task_id": task["task_id"]}),
            ("vortex_operator_cancel_task", {"task_id": task["task_id"], "reason": "x"}),
            ("vortex_operator_events", {}),
        ]
        for name, args in calls:
            with self.subTest(tool=name):
                with self.assertRaises(Exception) as caught:
                    self.call("hermes-1", name, args)
                self.assertIn("Operator privileges required", str(caught.exception))
        self.assertEqual(self.store.operator_get_task("claude-operator", task["task_id"])["status"], "open")

    def test_operator_token_can_oversee_and_cancel(self):
        task = self.store.create_task("hermes-1", "oversee", assigned_to="hermes-2")
        overview = self.call("claude-operator", "vortex_operator_overview")
        self.assertEqual(overview["task_counts"]["open"], 1)
        result = self.call(
            "claude-operator", "vortex_operator_cancel_task",
            {"task_id": task["task_id"], "reason": "operator test"},
        )
        self.assertEqual(result["status"], "cancelled")

    def test_server_rejects_operator_not_in_agent_ids(self):
        bad = Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=("ghost",),
            development_tokens={agent: f"token-{agent}" for agent in AGENTS},
        )
        with self.assertRaises(RuntimeError):
            create_server(VortexStore(":memory:", agents={a: a for a in AGENTS}), bad)


if __name__ == "__main__":
    unittest.main()
