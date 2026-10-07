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
