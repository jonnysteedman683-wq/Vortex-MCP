"""Per-agent API keys: stored only as hashes, refused when malformed, revoked within the cache TTL."""
import asyncio
import hashlib
import io
import logging
import os
import re
import subprocess
import sys
import unittest
import uuid
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from tests.backends import PG_URL, make_store
from vortex import keys as keys_module
from vortex.keys import KEY_PATTERN, ApiKeyTokenVerifier

KEY_SHAPE = re.compile(r"^vtx_[A-Za-z0-9_-]{43}$")
SCOPES = ["vortex:connect", "vortex:read", "vortex:message"]


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class CountingStore:
    """Wraps a store and counts key lookups, so tests can prove the database was (not) asked."""

    def __init__(self, store):
        self._store = store
        self.lookups = 0

    def agent_for_key(self, key):
        self.lookups += 1
        return self._store.agent_for_key(key)


def _mutate(key, index=20):
    """The same key with one character changed (still well-formed)."""
    replacement = "A" if key[index] != "A" else "B"
    return key[:index] + replacement + key[index + 1:]


class KeyTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.store = make_store(Path(self.temp.name) / "test.sqlite3")
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)

    def verify(self, verifier, token):
        return asyncio.run(verifier.verify_token(token))

    def stored_hashes(self):
        return [row["key_hash"] for row in self.store._db.execute("SELECT key_hash FROM agent_keys").fetchall()]


class StoreKeyTests(KeyTestCase):
    def test_issue_and_verify_round_trip(self):
        key = self.store.issue_key("hermes-1")
        self.assertRegex(key, KEY_SHAPE)
        self.assertEqual(self.store.agent_for_key(key), "hermes-1")
        access = self.verify(ApiKeyTokenVerifier(self.store, resource_url="https://v.example.test/mcp"), key)
        self.assertEqual(access.client_id, "hermes-1")
        self.assertEqual(access.subject, "hermes-1")
        self.assertEqual(access.scopes, SCOPES)
        self.assertEqual(access.resource, "https://v.example.test/mcp")
        self.assertIsNone(access.expires_at)

    def test_only_the_sha256_hash_is_stored(self):
        key = self.store.issue_key("hermes-1")
        self.assertEqual(self.stored_hashes(), [hashlib.sha256(key.encode("ascii")).hexdigest()])
        for table in ("agent_keys", "agents", "outbox", "operator_actions"):
            for row in self.store._db.execute(f"SELECT * FROM {table}").fetchall():
                self.assertNotIn(key, " ".join(str(v) for v in dict(row).values()))

    def test_unknown_agent_cannot_get_key(self):
        for agent in ("nobody-1", "", "hermes-1\x00"):
            with self.subTest(agent=agent), self.assertRaises(ValueError):
                self.store.issue_key(agent)
        self.assertEqual(self.stored_hashes(), [])

    def test_agent_may_hold_several_active_keys(self):
        first = self.store.issue_key("hermes-1")
        second = self.store.issue_key("hermes-1")
        self.assertNotEqual(first, second)
        self.assertEqual(self.store.agent_for_key(first), "hermes-1")
        self.assertEqual(self.store.agent_for_key(second), "hermes-1")

    def test_revoke_keys_revokes_only_that_agents_active_keys(self):
        first = self.store.issue_key("hermes-1")
        second = self.store.issue_key("hermes-1")
        other = self.store.issue_key("hermes-2")
        self.assertEqual(self.store.revoke_keys("hermes-1"), 2)
        self.assertIsNone(self.store.agent_for_key(first))
        self.assertIsNone(self.store.agent_for_key(second))
        self.assertEqual(self.store.agent_for_key(other), "hermes-2")
        self.assertEqual(self.store.revoke_keys("hermes-1"), 0)
        self.assertEqual(self.store.revoke_keys("nobody-1"), 0)

    def test_revocation_time_is_not_overwritten(self):
        self.store.issue_key("hermes-1")
        self.store.revoke_keys("hermes-1")
        self.store._db.execute("UPDATE agent_keys SET revoked_at='2000-01-01T00:00:00+00:00'")
        self.store.issue_key("hermes-1")
        self.assertEqual(self.store.revoke_keys("hermes-1"), 1)
        revoked = sorted(row["revoked_at"] for row in self.store.list_keys())
        self.assertEqual(revoked[0], "2000-01-01T00:00:00+00:00")

    def test_agent_for_key_refuses_malformed_input_without_error(self):
        self.store.issue_key("hermes-1")
        for bad in ("", "vtx_", "x" * 10_000, "vtx_\x00" + "a" * 42, None, 7, b"vtx_"):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertIsNone(self.store.agent_for_key(bad))

    def test_list_keys_hides_hashes(self):
        key = self.store.issue_key("hermes-1")
        self.store.issue_key("hermes-2")
        self.store.revoke_keys("hermes-2")
        listed = self.store.list_keys()
        self.assertEqual(len(listed), 2)
        full = set(self.stored_hashes())
        for entry in listed:
            self.assertEqual(set(entry), {"agent_id", "created_at", "revoked_at", "hash_prefix"})
            self.assertEqual(len(entry["hash_prefix"]), 8)
            self.assertTrue(any(h.startswith(entry["hash_prefix"]) for h in full))
            text = repr(entry)
            self.assertNotIn(key, text)
            for digest in full:
                self.assertNotIn(digest, text)
        by_agent = {entry["agent_id"]: entry for entry in listed}
        self.assertIsNone(by_agent["hermes-1"]["revoked_at"])
        self.assertIsNotNone(by_agent["hermes-2"]["revoked_at"])

    def test_key_not_in_logs(self):
        with self.assertLogs(level=logging.DEBUG) as captured:
            logging.getLogger("vortex.test").debug("capture is on")
            key = self.store.issue_key("hermes-1")
            verifier = ApiKeyTokenVerifier(self.store)
            access = self.verify(verifier, key)
            self.verify(verifier, _mutate(key))
            self.store.list_keys()
            self.store.revoke_keys("hermes-1")
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        text = "\n".join(captured.output) + repr(access) + repr(verifier) + repr(self.store)
        self.assertNotIn(key, text)
        self.assertNotIn(key[4:], text)
        self.assertNotIn(digest, text)


class VerifierTests(KeyTestCase):
    def test_revoked_key_stops_within_ttl(self):
        clock = FakeClock()
        counting = CountingStore(self.store)
        verifier = ApiKeyTokenVerifier(counting, ttl=30.0, clock=clock)
        key = self.store.issue_key("hermes-1")
        self.assertEqual(self.verify(verifier, key).client_id, "hermes-1")
        self.assertEqual(counting.lookups, 1)

        self.assertEqual(self.store.revoke_keys("hermes-1"), 1)
        clock.now += 29.0
        # Still inside the cache window: served from the cache, without asking the store.
        self.assertEqual(self.verify(verifier, key).client_id, "hermes-1")
        self.assertEqual(counting.lookups, 1)

        clock.now += 1.0  # exactly ttl seconds after the lookup: the entry has expired
        self.assertIsNone(self.verify(verifier, key))
        self.assertEqual(counting.lookups, 2)
        clock.now += 1000.0
        self.assertIsNone(self.verify(verifier, key))

    def test_cache_window_is_not_extended_by_use(self):
        clock = FakeClock()
        verifier = ApiKeyTokenVerifier(self.store, clock=clock)
        key = self.store.issue_key("hermes-1")
        self.verify(verifier, key)
        self.store.revoke_keys("hermes-1")
        for _ in range(5):  # a busy agent mid-session keeps using the key
            clock.now += 5.0
            self.assertIsNotNone(self.verify(verifier, key))
        clock.now += 5.0  # 30 s after the one real lookup
        self.assertIsNone(self.verify(verifier, key))

    def test_zero_ttl_revokes_on_the_next_request(self):
        verifier = ApiKeyTokenVerifier(self.store, ttl=0, clock=FakeClock())
        key = self.store.issue_key("hermes-1")
        self.assertIsNotNone(self.verify(verifier, key))
        self.store.revoke_keys("hermes-1")
        self.assertIsNone(self.verify(verifier, key))

    def test_ttl_cannot_exceed_thirty_seconds(self):
        for ttl in (30.01, 3600, -1, float("nan"), float("inf")):
            with self.subTest(ttl=ttl), self.assertRaises(ValueError):
                ApiKeyTokenVerifier(self.store, ttl=ttl)

    def test_negative_lookups_are_not_cached(self):
        counting = CountingStore(self.store)
        verifier = ApiKeyTokenVerifier(counting, clock=FakeClock())
        key = self.store.issue_key("hermes-1")
        self.store.revoke_keys("hermes-1")
        self.assertIsNone(self.verify(verifier, key))
        self.assertIsNone(self.verify(verifier, key))
        self.assertEqual(counting.lookups, 2)
        self.assertEqual(verifier._cache, {})

    def test_malformed_keys_never_reach_the_store(self):
        counting = CountingStore(self.store)
        verifier = ApiKeyTokenVerifier(counting)
        good = self.store.issue_key("hermes-1")
        body = good[4:]
        malformed = [
            "",
            " ",
            body,                      # no prefix
            "vtx_",
            "VTX_" + body,             # wrong prefix
            "vty_" + body,
            "xvtx_" + body,
            "vtx_" + body[:42],        # 42 characters
            "vtx_" + body + "a",       # 44 characters
            good + "\n",               # a trailing newline must not slip past the pattern
            " " + good,
            "vtx_" + body[:42] + "\x00",
            "vtx_" + body[:42] + "+",
            "vtx_" + body[:42] + "é",
            "vtx_" + "a" * 10_000,
            "a" * 10_000,
            None,
            good.encode("ascii"),
        ]
        for token in malformed:
            with self.subTest(token=repr(token)[:24]):
                self.assertIsNone(self.verify(verifier, token))
        self.assertEqual(counting.lookups, 0)
        self.assertEqual(self.verify(verifier, good).client_id, "hermes-1")
        self.assertEqual(counting.lookups, 1)

    def test_near_miss_key_is_refused(self):
        counting = CountingStore(self.store)
        verifier = ApiKeyTokenVerifier(counting)
        theirs = self.store.issue_key("hermes-2")
        self.store.issue_key("hermes-1")
        for index in (4, 20, 46):
            near = _mutate(theirs, index)
            self.assertRegex(near, KEY_SHAPE)
            with self.subTest(index=index):
                self.assertIsNone(self.verify(verifier, near))
                self.assertIsNone(self.store.agent_for_key(near))
        self.assertEqual(counting.lookups, 3)  # well-formed, so each was hashed and looked up
        # A hit in the cache for the real key must not make a near miss pass either.
        self.assertEqual(self.verify(verifier, theirs).client_id, "hermes-2")
        self.assertIsNone(self.verify(verifier, _mutate(theirs)))

    def test_cache_holds_no_raw_key(self):
        verifier = ApiKeyTokenVerifier(self.store)
        key = self.store.issue_key("hermes-1")
        access = self.verify(verifier, key)
        self.assertNotIn(key, repr(vars(verifier)))
        self.assertNotIn(key, repr(access))
        self.assertNotIn(key, access.model_dump_json())

    def test_database_outage_fails_closed_without_leaking(self):
        class Down:
            def agent_for_key(self, key):
                raise ConnectionError("database unavailable")

        key = self.store.issue_key("hermes-1")
        with self.assertLogs("vortex.keys", level=logging.WARNING) as captured:
            self.assertIsNone(self.verify(ApiKeyTokenVerifier(Down()), key))
        self.assertNotIn(key, "\n".join(captured.output))

    def test_pattern_is_the_documented_one(self):
        self.assertEqual(KEY_PATTERN.pattern, r"vtx_[A-Za-z0-9_-]{43}")


class KeysCliTests(KeyTestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        # The CLI closes the store it opened; keep this one open for the assertions.
        with (
            patch.object(keys_module, "_open_store", return_value=self.store) as opened,
            patch.object(self.store, "close") as closed,
            redirect_stdout(out),
        ):
            code = keys_module.main(list(argv))
        self.assertEqual(closed.call_count, opened.call_count)
        return code, out.getvalue(), opened

    def test_add_prints_the_key_exactly_once(self):
        code, out, _ = self.run_cli("add", "hermes-1")
        self.assertEqual(code, 0)
        found = re.findall(r"vtx_[A-Za-z0-9_-]{43}", out)
        self.assertEqual(len(found), 1)
        self.assertEqual(self.store.agent_for_key(found[0]), "hermes-1")
        self.assertIn("store this now", out.lower())

    def test_add_for_unknown_agent_fails_and_issues_nothing(self):
        code, out, _ = self.run_cli("add", "nobody-1")
        self.assertEqual(code, 1)
        self.assertNotIn("vtx_", out)
        self.assertEqual(self.store.list_keys(), [])

    def test_revoke_reports_the_count(self):
        key = self.store.issue_key("hermes-1")
        self.store.issue_key("hermes-1")
        code, out, _ = self.run_cli("revoke", "hermes-1")
        self.assertEqual(code, 0)
        self.assertIn("2", out)
        self.assertIsNone(self.store.agent_for_key(key))
        code, out, _ = self.run_cli("revoke", "hermes-1")
        self.assertEqual(code, 1)

    def test_list_shows_no_key_and_no_full_hash(self):
        key = self.store.issue_key("hermes-1")
        self.store.issue_key("hermes-2")
        self.store.revoke_keys("hermes-2")
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("hermes-1", out)
        self.assertIn("hermes-2", out)
        self.assertIn("revoked", out)
        self.assertNotIn(key, out)
        for digest in self.stored_hashes():
            self.assertNotIn(digest, out)
            self.assertIn(digest[:8], out)

    def test_bad_usage_exits_2_without_opening_the_store(self):
        for argv in ((), ("add",), ("revoke",), ("list", "extra"), ("add", "a", "b"), ("frobnicate", "x")):
            with self.subTest(argv=argv):
                code, out, opened = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn("usage", out.lower())
                opened.assert_not_called()

    def test_cli_requires_a_postgres_store_url_and_does_not_echo_it(self):
        out = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), redirect_stdout(out):
            self.assertEqual(keys_module.main(["list"]), 2)
        self.assertIn("VORTEX_STORE_URL", out.getvalue())
        marker = "not-a-postgres-url-" + "x" * 8
        out = io.StringIO()
        with patch.dict("os.environ", {"VORTEX_STORE_URL": marker}, clear=True), redirect_stdout(out):
            self.assertEqual(keys_module.main(["list"]), 2)
        self.assertNotIn(marker, out.getvalue())

    def test_database_outage_is_reported_without_details(self):
        out = io.StringIO()
        with (
            patch.object(keys_module, "_open_store", side_effect=ConnectionError("database unavailable")),
            redirect_stdout(out),
        ):
            self.assertEqual(keys_module.main(["list"]), 1)
        self.assertEqual(out.getvalue().strip(), "database unavailable")

    def test_python_m_vortex_dispatches_keys(self):
        from vortex import __main__ as entry

        with (
            patch.object(entry.sys, "argv", ["vortex", "keys", "list"]),
            patch.object(keys_module, "main", return_value=7) as called,
        ):
            self.assertEqual(entry.main(), 7)
        called.assert_called_once_with(["list"])


@unittest.skipUnless(PG_URL, "needs VORTEX_TEST_DATABASE_URL")
class KeysCommandOnPostgresTests(unittest.TestCase):
    """The real command, in its own process, against a real schema."""

    def setUp(self):
        self.store = make_store(agents={"claude-1": "claude-1"})
        self.addCleanup(self.store.close)
        self.store.acquire_instance_lock()  # a live server holds this; the command must still work
        self.schema = self.store._db.schema

    def run_cli(self, *argv, **env):
        base = {
            "PATH": os.environ.get("PATH", ""),
            "VORTEX_STORE_URL": PG_URL,
            "VORTEX_SCHEMA": self.schema,
            "VORTEX_AGENT_IDS": "claude-1,hermes-1",
        }
        base.update(env)
        return subprocess.run(
            [sys.executable, "-m", "vortex", "keys", *argv],
            env=base, capture_output=True, text=True, timeout=60, check=False,
        )

    def test_add_list_revoke_beside_a_running_server(self):
        added = self.run_cli("add", "claude-1")
        self.assertEqual(added.returncode, 0, added.stderr)
        found = re.findall(r"vtx_[A-Za-z0-9_-]{43}", added.stdout + added.stderr)
        self.assertEqual(len(found), 1)
        self.assertIn("Store this now", added.stdout)
        key = found[0]
        self.assertEqual(self.store.agent_for_key(key), "claude-1")

        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("claude-1", listed.stdout)
        self.assertIn("active", listed.stdout)
        digest = hashlib.sha256(key.encode("ascii")).hexdigest()
        for text in (listed.stdout, listed.stderr):
            self.assertNotIn(key, text)
            self.assertNotIn(digest, text)

        revoked = self.run_cli("revoke", "claude-1")
        self.assertEqual(revoked.returncode, 0, revoked.stderr)
        self.assertIsNone(self.store.agent_for_key(key))
        self.assertIn("revoked", self.run_cli("list").stdout)

    def test_unknown_agent_and_unreachable_database_do_not_leak(self):
        unknown = self.run_cli("add", "nobody-1")
        self.assertEqual(unknown.returncode, 1)
        self.assertNotIn("vtx_", unknown.stdout + unknown.stderr)

        word = uuid.uuid4().hex
        userinfo = ":".join(("u" + word[:8], word))  # joined so no credential-shaped literal
        dead = "postgresql://" + "@".join((userinfo, "127.0.0.1:1/postgres"))  # noqa: FLY002 - same reason
        down = self.run_cli("list", VORTEX_STORE_URL=dead)
        self.assertEqual(down.returncode, 1)
        self.assertNotIn(word, down.stdout + down.stderr)
        self.assertNotIn("Traceback", down.stderr)


if __name__ == "__main__":
    unittest.main()
