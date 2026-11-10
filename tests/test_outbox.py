"""The outbox is written in the same transaction as every task event and operator action."""
import json
import unittest
from unittest.mock import patch

from tests.backends import make_store

AGENTS = ("hermes-1", "hermes-2", "claude-operator")


class OutboxTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(agents={a: a for a in AGENTS})
        self.addCleanup(self.store.close)

    def _rows(self, sql):
        return [dict(r) for r in self.store._db.execute(sql).fetchall()]

    def test_every_task_event_has_one_outbox_row(self):
        task = self.store.create_task("hermes-1", "build it", assigned_to="hermes-2")
        self.store.claim_task("hermes-2", task["task_id"])
        self.store.complete_task("hermes-2", task["task_id"], "done")
        events = self._rows("SELECT event_id FROM task_events ORDER BY event_id")
        outbox = [r for r in self.store.outbox_unsent() if r["event_kind"].startswith("task.")]
        self.assertEqual(len(outbox), len(events))
        self.assertGreaterEqual(len(events), 3)
        self.assertEqual([r["payload"]["event_id"] for r in outbox], [e["event_id"] for e in events])

    def test_operator_action_writes_outbox(self):
        self.store.operator_pause_agent("claude-operator", "hermes-2", "testing")
        rows = [r for r in self.store.outbox_unsent() if r["event_kind"] == "operator.pause_agent"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["target_id"], "hermes-2")

    def test_failed_write_leaves_no_outbox_row(self):
        before = len(self._rows("SELECT event_id FROM task_events"))
        with patch.object(type(self.store), "_emit", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError):
            self.store.create_task("hermes-1", "never lands", assigned_to="hermes-2")
        self.assertEqual(len(self._rows("SELECT event_id FROM task_events")), before)
        self.assertEqual(self.store.outbox_unsent(), [])
        self.assertEqual(self.store.list_tasks("hermes-1"), [])

    def test_mark_sent_removes_row_from_unsent(self):
        self.store.create_task("hermes-1", "one", assigned_to="hermes-2")
        first = self.store.outbox_unsent()[0]
        self.store.outbox_mark_sent(first["outbox_id"])
        self.assertNotIn(first["outbox_id"], [r["outbox_id"] for r in self.store.outbox_unsent()])
        self.assertEqual(json.loads(json.dumps(first["payload"]))["task_id"], first["payload"]["task_id"])
