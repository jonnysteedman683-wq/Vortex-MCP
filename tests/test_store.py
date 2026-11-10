"""Tests for Vortex message persistence and isolation."""
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from tests.backends import make_store, sqlite_only
from vortex.store import VortexStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = make_store(Path(self.temp.name) / "test.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def test_four_default_agent_instances_are_separate(self):
        agents = self.store.list_agents("hermes-1")
        self.assertEqual({a["agent_id"] for a in agents},
                         {"hermes-1", "hermes-2", "antigravity-1", "spark-1"})
        self.assertTrue(next(a for a in agents if a["agent_id"] == "hermes-1")["is_self"])

    def test_register_heartbeat_and_online_expiry(self):
        self.store.register("hermes-1")
        statuses = {a["agent_id"]: a["status"] for a in self.store.list_agents("hermes-1")}
        self.assertEqual(statuses["hermes-1"], "online")
        self.assertEqual(statuses["spark-1"], "offline")

    def test_message_delivery_ack_and_threaded_reply(self):
        original = self.store.send("hermes-1", "spark-1", "Hello Spark")
        self.assertEqual(len(self.store.inbox("spark-1")), 1)
        self.assertEqual(self.store.inbox("hermes-1"), [])
        reply = self.store.send("spark-1", "hermes-1", "Hi Hermes", reply_to=original["message_id"])
        self.assertEqual(reply["thread_id"], original["thread_id"])
        self.assertEqual(len(self.store.thread("hermes-1", original["message_id"])), 2)
        acked = self.store.acknowledge("spark-1", original["message_id"])
        self.assertTrue(acked["acked_at"])
        self.assertEqual(self.store.inbox("spark-1"), [])
        self.assertEqual(len(self.store.inbox("spark-1", include_acked=True)), 1)

    def test_inbox_pages_are_stable_and_cursor_is_recipient_scoped(self):
        for index in range(5):
            self.store.send("hermes-1", "spark-1", f"Message {index}")

        messages = self.store.inbox("spark-1", include_acked=True)
        first_page = self.store.inbox("spark-1", limit=2, include_acked=True)
        second_page = self.store.inbox(
            "spark-1", limit=2, include_acked=True,
            after_message_id=first_page[-1]["message_id"],
        )
        final_page = self.store.inbox(
            "spark-1", include_acked=True,
            after_message_id=second_page[-1]["message_id"],
        )
        self.assertEqual(first_page + second_page + final_page, messages)
        with self.assertRaises(PermissionError):
            self.store.inbox("hermes-1", after_message_id=messages[0]["message_id"])
        with self.assertRaises(ValueError):
            self.store.inbox("spark-1", after_message_id="missing")

    @sqlite_only
    def test_initialize_removes_redundant_inbox_index(self):
        with self.store._db:
            self.store._db.execute(
                "CREATE INDEX idx_messages_inbox ON messages(recipient_id, acked_at, created_at)"
            )

        self.store._initialize()

        indexes = {
            row["name"] for row in self.store._db.execute("PRAGMA index_list(messages)")
        }
        self.assertNotIn("idx_messages_inbox", indexes)
        self.assertIn("idx_messages_inbox_cursor", indexes)

    @sqlite_only
    def test_pending_approval_index_matches_filter_and_order(self):
        with self.store._db:
            self.store._db.execute(
                "CREATE INDEX idx_tasks_approval ON tasks(approval_state, created_at)"
            )

        self.store._initialize()

        indexes = {
            row["name"] for row in self.store._db.execute("PRAGMA index_list(tasks)")
        }
        self.assertNotIn("idx_tasks_approval", indexes)
        self.assertIn("idx_tasks_pending_approval", indexes)
        plan = self.store._db.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT * FROM tasks
            WHERE approval_state='pending' AND status='open'
            ORDER BY priority,created_at,task_id LIMIT 50
            """
        ).fetchall()
        details = " ".join(row["detail"] for row in plan)
        self.assertIn("idx_tasks_pending_approval", details)
        self.assertNotIn("TEMP B-TREE", details)

    def test_bulk_acknowledgement_is_atomic_and_idempotent(self):
        first = self.store.send("hermes-1", "spark-1", "First")
        second = self.store.send("hermes-2", "spark-1", "Second")
        third = self.store.send("hermes-1", "hermes-2", "Not Spark's")

        with self.assertRaises(PermissionError):
            self.store.acknowledge_many(
                "spark-1", [first["message_id"], third["message_id"]]
            )
        self.assertEqual(len(self.store.inbox("spark-1")), 2)

        acknowledged = self.store.acknowledge_many(
            "spark-1", [first["message_id"], second["message_id"]]
        )
        self.assertTrue(all(message["acked_at"] for message in acknowledged))
        again = self.store.acknowledge_many(
            "spark-1", [first["message_id"], second["message_id"]]
        )
        self.assertEqual(
            [message["acked_at"] for message in acknowledged],
            [message["acked_at"] for message in again],
        )
        self.assertEqual(self.store.inbox("spark-1"), [])

    def test_bulk_acknowledgement_rejects_invalid_batches(self):
        message = self.store.send("hermes-1", "spark-1", "Hello")
        for message_ids in (
            [],
            [message["message_id"]] * 2,
            ["missing"],
        ):
            with self.subTest(message_ids=message_ids), self.assertRaises(ValueError):
                self.store.acknowledge_many("spark-1", message_ids)
        with self.assertRaises(ValueError):
            self.store.acknowledge_many("spark-1", ["x"] * 101)
        self.assertEqual(len(self.store.inbox("spark-1")), 1)

    def test_unknown_agent_cannot_register_or_receive(self):
        with self.assertRaises(ValueError):
            self.store.register("rogue")
        with self.assertRaises(ValueError):
            self.store.send("hermes-1", "rogue", "hello")

    def test_empty_oversized_and_self_messages_rejected(self):
        for sender, recipient, body in (
            ("hermes-1", "spark-1", " "),
            ("hermes-1", "spark-1", "x" * 8001),
            ("hermes-1", "hermes-1", "self"),
        ):
            with self.subTest(body_length=len(body)), self.assertRaises(ValueError):
                self.store.send(sender, recipient, body)

    def test_nonparticipant_cannot_join_existing_thread_by_id(self):
        original = self.store.send("hermes-1", "spark-1", "Private thread")
        with self.assertRaises(ValueError):
            self.store.send("antigravity-1", "hermes-1", "Injected", thread_id=original["thread_id"])
        self.assertEqual(len(self.store.thread("hermes-1", original["message_id"])), 1)

    def test_only_recipient_can_acknowledge(self):
        message = self.store.send("hermes-1", "spark-1", "Hello")
        with self.assertRaises(PermissionError):
            self.store.acknowledge("hermes-1", message["message_id"])

    def test_nonparticipant_cannot_reply_or_read_thread(self):
        message = self.store.send("hermes-1", "spark-1", "Hello")
        with self.assertRaises(PermissionError):
            self.store.send("antigravity-1", "hermes-1", "Injected", reply_to=message["message_id"])
        with self.assertRaises(PermissionError):
            self.store.thread("antigravity-1", message["message_id"])

    def test_mass_dispatch_creates_one_task_per_recipient(self):
        tasks = self.store.dispatch_tasks(
            "hermes-1",
            "Prepare launch brief",
            ["spark-1", "hermes-2"],
            priority=2,
        )
        self.assertEqual([task["assigned_to"] for task in tasks], ["spark-1", "hermes-2"])
        self.assertEqual([task["goal"] for task in tasks], ["Prepare launch brief", "Prepare launch brief"])
        self.assertEqual(len(self.store.list_tasks("hermes-1")), 2)

        with self.assertRaisesRegex(ValueError, "at least one recipient"):
            self.store.dispatch_tasks("hermes-1", "Invalid batch", [])
        before = len(self.store.list_tasks("hermes-1"))
        with self.assertRaisesRegex(ValueError, "Unknown recipient agent identity"):
            self.store.dispatch_tasks("hermes-1", "Invalid batch", ["spark-1", "rogue"])
        self.assertEqual(len(self.store.list_tasks("hermes-1")), before)

    def test_task_creation_is_idempotent_and_rejects_key_reuse(self):
        task = self.store.create_task(
            "hermes-1", "  Review MCP task support  ", assigned_to="spark-1",
            priority=1, idempotency_key="review-tasks",
        )
        retry = self.store.create_task(
            "hermes-1", "Review MCP task support", assigned_to="spark-1",
            priority=1, idempotency_key="review-tasks",
        )
        self.assertEqual(retry["task_id"], task["task_id"])
        self.assertEqual(len(self.store.list_tasks("hermes-1")), 1)
        with self.assertRaisesRegex(ValueError, "different task"):
            self.store.create_task(
                "hermes-1", "Different goal", assigned_to="spark-1",
                priority=1, idempotency_key="review-tasks",
            )

    def test_task_dependencies_gate_claims_and_open_work_can_be_claimed(self):
        prerequisite = self.store.create_task(
            "hermes-1", "Prepare source material", assigned_to="hermes-1"
        )
        follow_up = self.store.create_task(
            "hermes-1", "Write the report", assigned_to="spark-1",
            depends_on=[prerequisite["task_id"]],
        )
        self.assertFalse(follow_up["ready"])
        with self.assertRaisesRegex(ValueError, "waiting for dependency"):
            self.store.claim_task("spark-1", follow_up["task_id"])

        self.store.claim_task("hermes-1", prerequisite["task_id"])
        self.store.complete_task("hermes-1", prerequisite["task_id"], "Source ready")
        refreshed = self.store.get_task("spark-1", follow_up["task_id"])
        self.assertTrue(refreshed["ready"])
        claimed = self.store.claim_task("spark-1", follow_up["task_id"])
        self.assertEqual(claimed["status"], "in_progress")
        self.assertEqual(claimed["assigned_to"], "spark-1")

    def test_task_access_and_assignment_are_enforced(self):
        task = self.store.create_task(
            "hermes-1", "Private task", assigned_to="spark-1"
        )
        with self.assertRaises(PermissionError):
            self.store.get_task("antigravity-1", task["task_id"])
        with self.assertRaises(PermissionError):
            self.store.claim_task("antigravity-1", task["task_id"])
        with self.assertRaises(PermissionError):
            self.store.complete_task("hermes-1", task["task_id"], "Not assigned")
        self.assertEqual(self.store.get_task("spark-1", task["task_id"])["status"], "open")

    def test_task_lease_progress_completion_and_event_history(self):
        task = self.store.create_task("hermes-1", "Run an experiment")
        claimed = self.store.claim_task("spark-1", task["task_id"], lease_seconds=60)
        self.assertEqual(claimed["assigned_to"], "spark-1")
        with self.assertRaisesRegex(ValueError, "Lease duration"):
            self.store.claim_task("spark-1", task["task_id"], lease_seconds=5)
        blocked = self.store.update_task(
            "spark-1", task["task_id"], "blocked", blocked_reason="Waiting on input"
        )
        self.assertEqual(blocked["blocked_reason"], "Waiting on input")
        resumed = self.store.claim_task("spark-1", task["task_id"])
        self.assertEqual(resumed["status"], "in_progress")
        completed = self.store.complete_task("spark-1", task["task_id"], "Experiment passed")
        self.assertEqual(completed["result"], "Experiment passed")
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [event["event_type"] for event in self.store.task_events("hermes-1", task["task_id"])],
            ["created", "claimed", "blocked", "claimed", "completed"],
        )

    def test_expired_task_lease_must_be_renewed_before_completion(self):
        task = self.store.create_task("hermes-1", "Long-running work")
        self.store.claim_task("spark-1", task["task_id"], lease_seconds=30)
        with self.store._db:
            self.store._db.execute(
                "UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                (task["task_id"],),
            )
        with self.assertRaisesRegex(ValueError, "lease expired"):
            self.store.complete_task("spark-1", task["task_id"], "Too late")
        self.store.claim_task("spark-1", task["task_id"], lease_seconds=30)
        self.assertEqual(
            self.store.complete_task("spark-1", task["task_id"], "Renewed and done")["status"],
            "completed",
        )

    def test_expired_lease_can_be_reclaimed_but_active_lease_cannot(self):
        task = self.store.create_task("hermes-1", "Recover abandoned work")
        self.store.claim_task("spark-1", task["task_id"], lease_seconds=30)
        with self.assertRaises(PermissionError):
            self.store.claim_task("hermes-2", task["task_id"])
        with self.store._db:
            self.store._db.execute(
                "UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE task_id=?",
                (task["task_id"],),
            )
        recovered = self.store.claim_task("hermes-2", task["task_id"])
        self.assertEqual(recovered["assigned_to"], "hermes-2")
        with self.assertRaises(PermissionError):
            self.store.complete_task("spark-1", task["task_id"], "Stale worker")
        events = self.store.task_events("hermes-1", task["task_id"])
        self.assertEqual(events[-1]["details"]["reclaimed_from"], "spark-1")

    def test_unassigned_open_tasks_are_discoverable_by_workers(self):
        task = self.store.create_task("hermes-1", "Available for a worker")
        self.assertEqual(
            [item["task_id"] for item in self.store.list_tasks("spark-1")],
            [task["task_id"]],
        )
        self.assertEqual(self.store.get_task("spark-1", task["task_id"])["goal"], "Available for a worker")

    @sqlite_only
    def test_list_tasks_loads_dependencies_in_one_query(self):
        parent = self.store.create_task("hermes-1", "Parent")
        self.store.create_task("hermes-1", "Child one", depends_on=[parent["task_id"]])
        self.store.create_task("hermes-1", "Child two", depends_on=[parent["task_id"]])
        statements = []
        self.store._db.set_trace_callback(statements.append)
        try:
            tasks = self.store.list_tasks("hermes-1")
        finally:
            self.store._db.set_trace_callback(None)

        dependency_queries = [
            statement for statement in statements
            if "from task_dependencies d" in statement.casefold()
        ]
        self.assertEqual(len(tasks), 3)
        self.assertEqual(len(dependency_queries), 1)
        children = [task for task in tasks if task["goal"].startswith("Child")]
        self.assertTrue(all(task["depends_on"] == [parent["task_id"]] for task in children))
        self.assertTrue(all(task["blocked_by"] == [parent["task_id"]] for task in children))

    def test_task_messages_are_linked_and_access_checked(self):
        task = self.store.create_task(
            "hermes-1", "Review a design", assigned_to="spark-1"
        )
        message = self.store.send(
            "hermes-1", "spark-1", "Please review this", task_id=task["task_id"]
        )
        self.assertEqual(message["task_id"], task["task_id"])
        self.assertEqual(self.store.inbox("spark-1")[0]["task_id"], task["task_id"])
        reply = self.store.send(
            "spark-1", "hermes-1", "I will review it", reply_to=message["message_id"]
        )
        self.assertEqual(reply["task_id"], task["task_id"])
        self.assertEqual(
            [entry["task_id"] for entry in self.store.thread("hermes-1", message["message_id"])],
            [task["task_id"], task["task_id"]],
        )
        with self.assertRaises(PermissionError):
            self.store.send("antigravity-1", "spark-1", "Unauthorized", task_id=task["task_id"])

    @sqlite_only
    def test_existing_message_database_is_migrated_with_nullable_task_link(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(database)
            connection.executescript("""
                CREATE TABLE agents (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'offline',
                    last_seen TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL REFERENCES agents(agent_id),
                    recipient_id TEXT NOT NULL REFERENCES agents(agent_id),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT,
                    reply_to TEXT REFERENCES messages(message_id)
                );
            """)
            connection.close()
            migrated = VortexStore(database)
            try:
                columns = {
                    row["name"] for row in migrated._db.execute("PRAGMA table_info(messages)").fetchall()
                }
                self.assertIn("task_id", columns)
                self.assertEqual(migrated.inbox("spark-1"), [])
            finally:
                migrated.close()

    def test_creator_can_cancel_and_task_terminal_states_are_stable(self):
        task = self.store.create_task("hermes-1", "Cancel this")
        cancelled = self.store.cancel_task("hermes-1", task["task_id"], "No longer needed")
        self.assertEqual(cancelled["status"], "cancelled")
        with self.assertRaisesRegex(ValueError, "already cancelled"):
            self.store.cancel_task("hermes-1", task["task_id"])


if __name__ == "__main__":
    unittest.main()


class TextValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = make_store(Path(self.temp.name) / "test.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def test_nul_in_ids_and_keys_is_a_value_error_on_every_backend(self):
        bad = "a\x00b"
        with self.assertRaises(ValueError):
            self.store.create_task("hermes-1", "goal", idempotency_key=bad)
        with self.assertRaises(ValueError):
            self.store.get_task("hermes-1", bad)
        with self.assertRaises(ValueError):
            self.store.send("hermes-1", "spark-1", "hi", reply_to=bad)
        with self.assertRaises(ValueError):
            self.store.acknowledge("spark-1", bad)
        self.assertEqual(self.store.list_tasks("hermes-1"), [])
        self.store.send("hermes-1", "spark-1", "still works")
        self.assertEqual(len(self.store.inbox("spark-1")), 1)

    def test_nul_characters_are_rejected_before_writing(self):
        with self.assertRaisesRegex(ValueError, "NUL"):
            self.store.send("hermes-1", "spark-1", "a\x00b")
        self.assertEqual(self.store.inbox("spark-1"), [])
        with self.assertRaisesRegex(ValueError, "NUL"):
            self.store.create_task("hermes-1", "goal\x00")
        self.assertEqual(self.store.list_tasks("hermes-1"), [])
