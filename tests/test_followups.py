"""Follow-ups from the P1a review: secret-free settings repr, relay backoff cap, SQL comments, URL errors."""
import os
import unittest
import uuid
from unittest.mock import patch

from vortex import relay
from vortex.db import open_database, to_pyformat
from vortex.settings import Settings


class SettingsReprTests(unittest.TestCase):
    def test_development_tokens_are_not_in_repr(self):
        tokens = {a: uuid.uuid4().hex for a in ("hermes-1", "hermes-2", "antigravity-1", "spark-1")}
        env = {"VORTEX_AUTH_MODE": "development"}
        env.update({"VORTEX_TOKEN_" + a.upper().replace("-", "_"): t for a, t in tokens.items()})
        with patch.dict(os.environ, env, clear=True):
            text = repr(Settings.from_env())
        for token in tokens.values():
            self.assertNotIn(token, text)

    def test_oauth_client_secret_is_not_in_repr(self):
        secret = uuid.uuid4().hex
        settings = Settings(auth_mode="oauth", host="127.0.0.1", port=8000, data_dir=".", oauth_client_secret=secret)
        self.assertNotIn(secret, repr(settings))


class BackoffTests(unittest.TestCase):
    def test_backoff_survives_a_very_long_outage(self):
        class AlwaysFails:
            def outbox_unsent(self, batch):
                raise ConnectionError("down")

        waits = []

        def sleep(seconds):
            waits.append(seconds)
            if len(waits) >= 1200:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            relay.run(AlwaysFails(), object(), interval=2.0, max_backoff=300.0, sleep=sleep)
        self.assertEqual(len(waits), 1200)
        self.assertEqual(waits[-1], 300.0)


class PyformatCommentTests(unittest.TestCase):
    def test_apostrophe_in_line_comment_does_not_hide_later_placeholder(self):
        sql = "SELECT 1 -- what's this?\nWHERE a = ?"
        self.assertEqual(to_pyformat(sql), "SELECT 1 -- what's this?\nWHERE a = %s")

    def test_placeholder_inside_block_comment_is_left_alone(self):
        self.assertEqual(to_pyformat("SELECT /* a? b's */ ?"), "SELECT /* a? b's */ %s")


class MalformedUrlTests(unittest.TestCase):
    def test_malformed_url_raises_connection_error_without_echoing_it(self):
        marker = "m" + uuid.uuid4().hex[:10]
        url = "postgresql://localhost:1/" + marker + "?" + marker + "_bad_option=1"
        with self.assertRaises(ConnectionError) as caught:
            open_database(url, schema="forge")
        self.assertNotIn(marker, str(caught.exception))
