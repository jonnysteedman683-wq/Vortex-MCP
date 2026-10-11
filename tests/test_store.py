"""Tests for Vortex message persistence and isolation."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from vortex.store import VortexStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = VortexStore(Path(self.temp.name) / "test.sqlite3")
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


if __name__ == "__main__":
    unittest.main()
