"""The relay copies the outbox to the UniSync ledger exactly once."""
import logging
import unittest
from unittest.mock import patch

from tests.backends import make_store
from vortex import relay

AGENTS = ("hermes-1", "hermes-2")


class FakeLedger:
    def __init__(self, fail_on=None, always_fail=False):
        self.posts = []
        self.mem = {}
        self.fail_on = fail_on
        self.always_fail = always_fail

    def post(self, sender, kind, task_id="", **body):
        if self.always_fail or body.get("key") == self.fail_on:
            raise ConnectionError("gateway down")
        self.posts.append((sender, kind, body))
        if kind == "memory.write":
            self.mem[body["key"]] = body["value"]

    def memory(self, scope="shared"):
        return dict(self.mem) if scope == relay.SCOPE else {}


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.store = make_store(agents={a: a for a in AGENTS})
        self.addCleanup(self.store.close)

    def _three_events(self):
        for goal in ("one", "two", "three"):
            self.store.create_task("hermes-1", goal, assigned_to="hermes-2")
        return [r["outbox_id"] for r in self.store.outbox_unsent()][:3]

    def test_relays_each_row_once_in_order(self):
        ids = self._three_events()
        ledger = FakeLedger()
        self.assertEqual(relay.relay_once(self.store, ledger), len(ids))
        self.assertEqual([b["key"] for _, _, b in ledger.posts], [f"outbox/{i}" for i in ids])
        self.assertTrue(all(s == "vortex" and k == "memory.write" for s, k, _ in ledger.posts))
        self.assertEqual(ledger.posts[0][2]["scope"], "forge")
        self.assertEqual(ledger.posts[0][2]["value"]["kind"], "task.created")
        self.assertEqual(relay.relay_once(self.store, ledger), 0)
        self.assertEqual(len(ledger.posts), len(ids))

    def test_crash_after_post_does_not_duplicate(self):
        self._three_events()
        ledger = FakeLedger()
        real = self.store.outbox_mark_sent
        calls = {"n": 0}

        def flaky(outbox_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crashed before marking sent")
            real(outbox_id)

        with patch.object(self.store, "outbox_mark_sent", side_effect=flaky), self.assertRaises(RuntimeError):
            relay.relay_once(self.store, ledger)
        self.assertEqual(len(ledger.posts), 1)
        relay.relay_once(self.store, ledger)
        keys = [b["key"] for _, _, b in ledger.posts]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(self.store.outbox_unsent(), [])

    def test_post_failure_leaves_row_unsent_and_stops(self):
        ids = self._three_events()
        ledger = FakeLedger(fail_on=f"outbox/{ids[1]}")
        with self.assertRaises(ConnectionError):
            relay.relay_once(self.store, ledger)
        unsent = [r["outbox_id"] for r in self.store.outbox_unsent()]
        self.assertNotIn(ids[0], unsent)
        self.assertIn(ids[1], unsent)
        self.assertIn(ids[2], unsent)

    def test_run_backs_off_exponentially_and_caps(self):
        self._three_events()
        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 10:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            relay.run(self.store, FakeLedger(always_fail=True), interval=2.0, max_backoff=300.0, sleep=sleep)
        self.assertEqual(waits[:4], [4.0, 8.0, 16.0, 32.0])
        self.assertEqual(max(waits), 300.0)
        self.assertNotIn(0, waits)

    def test_logs_contain_no_payload(self):
        self.store.create_task("hermes-1", "secret-marker goal", assigned_to="hermes-2")
        with self.assertLogs("vortex.relay", level=logging.WARNING) as logs, self.assertRaises(KeyboardInterrupt):
            relay.run(self.store, FakeLedger(always_fail=True), sleep=self._stop_after_one())
        self.assertNotIn("secret-marker", "\n".join(logs.output))

    @staticmethod
    def _stop_after_one():
        def sleep(_):
            raise KeyboardInterrupt
        return sleep


class RealLedgerTests(unittest.TestCase):
    """The relay's posts must pass lestinxor's own ledger rules, not just a fake's."""

    def setUp(self):
        try:
            from lestinxor.unisync import Ledger
        except ImportError:  # pragma: no cover - lestinxor is in the dev group
            self.skipTest("lestinxor not installed")
        self.ledger = Ledger()
        self.store = make_store(agents={a: a for a in AGENTS})
        self.addCleanup(self.store.close)

    def test_real_ledger_accepts_relay_posts_and_dedupes(self):
        self.store.create_task("hermes-1", "one", assigned_to="hermes-2")
        self.store.create_task("hermes-1", "two", assigned_to="hermes-2")
        sent = relay.relay_once(self.store, self.ledger)
        view = self.ledger.memory(relay.SCOPE)
        self.assertEqual(len(view), sent)
        self.assertTrue(all(k.startswith("outbox/") for k in view))
        self.assertEqual(relay.relay_once(self.store, self.ledger), 0)
