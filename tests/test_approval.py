"""Tests for the approval gate: pending tasks are unclaimable until an operator decides."""
import asyncio
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mcp.server.auth.provider import AccessToken
from mcp.server.fastmcp.exceptions import ToolError

from vortex.server import create_server
from vortex.settings import Settings
from vortex.store import VortexStore

AGENTS = ("hermes-1", "hermes-2", "spark-1", "claude-operator")
OP = "claude-operator"


def _store(path, policy=None):
    return VortexStore(path, agents={a: a for a in AGENTS}, approval_required_for=policy)


class ApprovalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = _store(Path(self.temp.name) / "a.sqlite3")
        self.addCleanup(self.store.close)

    def test_default_tasks_need_no_approval(self):
        task = self.store.create_task("hermes-1", "routine", assigned_to="hermes-2")
        self.assertEqual(task["approval_state"], "not_required")
        self.assertTrue(task["ready"])
        self.assertFalse(task["awaiting_approval"])
        self.store.claim_task("hermes-2", task["task_id"])

    def test_requested_approval_blocks_claim_until_approved(self):
        task = self.store.create_task("hermes-1", "rm -rf build/", assigned_to="hermes-2", requires_approval=True)
        self.assertEqual(task["approval_state"], "pending")
        self.assertTrue(task["awaiting_approval"])
        self.assertFalse(task["ready"])
        with self.assertRaises(PermissionError) as caught:
            self.store.claim_task("hermes-2", task["task_id"])
        self.assertIn("awaiting operator approval", str(caught.exception))

        approved = self.store.operator_approve_task(OP, task["task_id"], note="looked safe")
        self.assertEqual(approved["approval_state"], "approved")
        self.assertTrue(approved["ready"])
        claimed = self.store.claim_task("hermes-2", task["task_id"])
        self.assertEqual(claimed["status"], "in_progress")

        events = [e["event_type"] for e in self.store.operator_get_task(OP, task["task_id"])["events"]]
        self.assertEqual(events, ["created", "approved", "claimed"])
        created = self.store.operator_get_task(OP, task["task_id"])["events"][0]
        self.assertTrue(created["details"]["requires_approval"])

    def test_unassigned_pending_task_cannot_be_claimed_by_anyone(self):
        task = self.store.create_task("hermes-1", "open risky work", requires_approval=True)
        for agent in ("hermes-2", "spark-1"):
            with self.assertRaises(PermissionError):
                self.store.claim_task(agent, task["task_id"])

    def test_reject_cancels_permanently_and_requires_reason(self):
        task = self.store.create_task("hermes-1", "wipe", assigned_to="hermes-2", requires_approval=True)
        with self.assertRaises(ValueError):
            self.store.operator_reject_task(OP, task["task_id"], "   ")
        rejected = self.store.operator_reject_task(OP, task["task_id"], "not today")
        self.assertEqual((rejected["status"], rejected["approval_state"]), ("cancelled", "rejected"))
        with self.assertRaises(ValueError):
            self.store.claim_task("hermes-2", task["task_id"])
        with self.assertRaises(ValueError):
            self.store.operator_approve_task(OP, task["task_id"])
        last = self.store.operator_get_task(OP, task["task_id"])["events"][-1]
        self.assertEqual((last["event_type"], last["actor_id"], last["details"]),
                         ("rejected", OP, {"reason": "not today"}))

    def test_only_pending_tasks_can_be_decided(self):
        task = self.store.create_task("hermes-1", "plain", assigned_to="hermes-2")
        with self.assertRaises(ValueError):
            self.store.operator_approve_task(OP, task["task_id"])
        gated = self.store.create_task("hermes-1", "gated", assigned_to="hermes-2", requires_approval=True)
        self.store.operator_approve_task(OP, gated["task_id"])
        with self.assertRaises(ValueError):
            self.store.operator_approve_task(OP, gated["task_id"])

    def test_cancelled_pending_task_cannot_be_decided(self):
        task = self.store.create_task("hermes-1", "gated", assigned_to="hermes-2", requires_approval=True)
        self.store.cancel_task("hermes-1", task["task_id"], reason="changed my mind")
        with self.assertRaises(ValueError):
            self.store.operator_approve_task(OP, task["task_id"])
        self.assertEqual(self.store.operator_list_pending_approvals(OP), [])

    def test_pending_list_and_overview_count(self):
        low = self.store.create_task("hermes-1", "low", assigned_to="hermes-2", priority=4, requires_approval=True)
        high = self.store.create_task("hermes-1", "high", assigned_to="spark-1", priority=1, requires_approval=True)
        self.store.create_task("hermes-1", "ungated", assigned_to="spark-1")
        pending = self.store.operator_list_pending_approvals(OP)
        self.assertEqual([t["task_id"] for t in pending], [high["task_id"], low["task_id"]])
        self.assertEqual(self.store.operator_overview(OP)["pending_approvals"], 2)

    def test_idempotency_respects_approval_requirement(self):
        first = self.store.create_task(
            "hermes-1", "same", assigned_to="hermes-2", idempotency_key="k", requires_approval=True
        )
        with self.assertRaises(ValueError):
            self.store.create_task("hermes-1", "same", assigned_to="hermes-2", idempotency_key="k")
        self.store.operator_approve_task(OP, first["task_id"])
        replay = self.store.create_task(
            "hermes-1", "same", assigned_to="hermes-2", idempotency_key="k", requires_approval=True
        )
        self.assertEqual(replay["task_id"], first["task_id"])
        self.assertEqual(replay["approval_state"], "approved")

    def test_dispatch_gates_every_recipient(self):
        tasks = self.store.dispatch_tasks(
            "hermes-1", "fleet-wide restart", ["hermes-2", "spark-1"], requires_approval=True
        )
        self.assertEqual({t["approval_state"] for t in tasks}, {"pending"})

    def test_requires_approval_must_be_boolean(self):
        with self.assertRaises(ValueError):
            self.store.create_task("hermes-1", "x", requires_approval="yes")


class ApprovalPolicyTests(unittest.TestCase):
    def test_wildcard_policy_gates_all_and_cannot_be_opted_out(self):
        store = _store(":memory:", policy=("*",))
        self.addCleanup(store.close)
        task = store.create_task("hermes-1", "anything", assigned_to="hermes-2", requires_approval=False)
        self.assertEqual(task["approval_state"], "pending")
        unassigned = store.create_task("hermes-1", "unassigned")
        self.assertEqual(unassigned["approval_state"], "pending")

    def test_assignee_policy_gates_only_that_assignee(self):
        store = _store(":memory:", policy=("hermes-2",))
        self.addCleanup(store.close)
        self.assertEqual(store.create_task("hermes-1", "a", assigned_to="hermes-2")["approval_state"], "pending")
        self.assertEqual(store.create_task("hermes-1", "b", assigned_to="spark-1")["approval_state"], "not_required")

    def test_existing_database_is_migrated_and_old_tasks_stay_claimable(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            old = sqlite3.connect(path)
            old.executescript("""
                CREATE TABLE agents (agent_id TEXT PRIMARY KEY, display_name TEXT NOT NULL,
                    platform TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'offline', last_seen TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE tasks (task_id TEXT PRIMARY KEY, goal TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES agents(agent_id),
                    assigned_to TEXT REFERENCES agents(agent_id),
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','blocked','completed','cancelled')),
                    priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, lease_expires_at TEXT,
                    blocked_reason TEXT, result TEXT, idempotency_key TEXT,
                    UNIQUE(created_by, idempotency_key));
                INSERT INTO agents VALUES ('hermes-1','h1','hermes','offline',NULL,'2026-01-01T00:00:00+00:00');
                INSERT INTO agents VALUES ('hermes-2','h2','hermes','offline',NULL,'2026-01-01T00:00:00+00:00');
                INSERT INTO tasks(task_id,goal,created_by,assigned_to,created_at,updated_at)
                    VALUES ('legacy','old work','hermes-1','hermes-2',
                            '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00');
            """)
            old.commit()
            old.close()
            store = _store(path)
            try:
                legacy = store.get_task("hermes-2", "legacy")
                self.assertEqual(legacy["approval_state"], "not_required")
                self.assertEqual(store.claim_task("hermes-2", "legacy")["status"], "in_progress")
            finally:
                store.close()


class ApprovalSettingsTests(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "VORTEX_AUTH_MODE": "development",
            "VORTEX_AGENT_IDS": ",".join(AGENTS),
            **{f"VORTEX_TOKEN_{a.upper().replace('-', '_')}": f"token-{a}" for a in AGENTS},
        }
        values.update(overrides)
        return values

    def test_policy_parsing_and_fail_closed_validation(self):
        with patch.dict(os.environ, self.env(), clear=True):
            self.assertEqual(Settings.from_env().approval_required_for, ())
        with patch.dict(os.environ, self.env(VORTEX_OPERATOR_IDS=OP, VORTEX_APPROVAL_REQUIRED_FOR="*"), clear=True):
            self.assertEqual(Settings.from_env().approval_required_for, ("*",))
        with patch.dict(
            os.environ, self.env(VORTEX_OPERATOR_IDS=OP, VORTEX_APPROVAL_REQUIRED_FOR="hermes-2,spark-1"), clear=True
        ):
            self.assertEqual(Settings.from_env().approval_required_for, ("hermes-2", "spark-1"))
        for bad in ("ghost", "*,hermes-2", "hermes-2,hermes-2"):
            with (
                self.subTest(policy=bad),
                patch.dict(os.environ, self.env(VORTEX_OPERATOR_IDS=OP, VORTEX_APPROVAL_REQUIRED_FOR=bad), clear=True),
                self.assertRaises(RuntimeError),
            ):
                Settings.from_env()

    def test_policy_without_an_operator_is_rejected(self):
        with (
            patch.dict(os.environ, self.env(VORTEX_APPROVAL_REQUIRED_FOR="*"), clear=True),
            self.assertRaises(RuntimeError),
        ):
            Settings.from_env()


class ApprovalServerTests(unittest.TestCase):
    def setUp(self):
        self.store = _store(":memory:")
        self.addCleanup(self.store.close)
        settings = Settings(
            auth_mode="development", host="127.0.0.1", port=8000, data_dir=".vortex-data",
            agent_ids=AGENTS, operator_ids=(OP,), approval_required_for=("hermes-2",),
            development_tokens={a: f"token-{a}" for a in AGENTS},
        )
        self.server = create_server(self.store, settings)

    def call(self, identity, name, args=None):
        access = AccessToken(token="t", client_id=identity, subject=identity,
                             scopes=["vortex:connect", "vortex:read", "vortex:message"],
                             resource="http://127.0.0.1:8000/mcp")
        with patch("vortex.server.get_access_token", return_value=access):
            return asyncio.run(self.server._tool_manager.call_tool(name, args or {}))

    def test_server_applies_policy_to_a_provided_store(self):
        task = self.call("hermes-1", "vortex_create_task", {"goal": "deploy", "assigned_to": "hermes-2"})
        self.assertEqual(task["approval_state"], "pending")

    def test_non_operator_cannot_list_approve_or_reject(self):
        task = self.store.create_task("hermes-1", "gated", assigned_to="hermes-2", requires_approval=True)
        for name, args in [
            ("vortex_operator_list_pending_approvals", {}),
            ("vortex_operator_approve_task", {"task_id": task["task_id"]}),
            ("vortex_operator_reject_task", {"task_id": task["task_id"], "reason": "x"}),
        ]:
            with self.subTest(tool=name):
                for identity in ("hermes-1", "hermes-2"):
                    with self.assertRaises(Exception) as caught:
                        self.call(identity, name, args)
                    self.assertIn("Operator privileges required", str(caught.exception))
        self.assertEqual(self.store.operator_get_task(OP, task["task_id"])["approval_state"], "pending")

    def test_operator_approves_then_assignee_claims_via_tools(self):
        task = self.call("hermes-1", "vortex_create_task", {
            "goal": "migrate db", "assigned_to": "spark-1", "requires_approval": True,
        })
        with self.assertRaisesRegex(ToolError, "awaiting operator approval"):
            self.call("spark-1", "vortex_claim_task", {"task_id": task["task_id"]})
        pending = self.call(OP, "vortex_operator_list_pending_approvals")
        self.assertEqual([t["task_id"] for t in pending], [task["task_id"]])
        self.call(OP, "vortex_operator_approve_task", {"task_id": task["task_id"], "note": "ok"})
        claimed = self.call("spark-1", "vortex_claim_task", {"task_id": task["task_id"]})
        self.assertEqual(claimed["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()
