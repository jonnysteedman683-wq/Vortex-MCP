"""Tests for the Database layer under VortexStore."""
import unittest

from vortex.db import open_database


class SqliteDatabaseTests(unittest.TestCase):
    def test_sqlite_open_returns_mapping_rows(self):
        db = open_database(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE t(a TEXT)")
        db.execute("INSERT INTO t(a) VALUES(?)", ("x",))
        row = db.execute("SELECT a FROM t").fetchone()
        self.assertEqual(row["a"], "x")
        self.assertEqual(dict(row), {"a": "x"})
        self.assertEqual(db.dialect, "sqlite")
