"""Tests for the Database layer under VortexStore."""
import os
import unittest
import uuid

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


PG_URL = os.environ.get("VORTEX_TEST_DATABASE_URL")


@unittest.skipUnless(PG_URL, "needs VORTEX_TEST_DATABASE_URL")
class PostgresDatabaseTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.schema = "t_" + uuid.uuid4().hex
        self.admin = psycopg.connect(PG_URL, autocommit=True)
        self.admin.execute(f'CREATE SCHEMA "{self.schema}"')
        self.addCleanup(self.admin.close)
        self.addCleanup(self.admin.execute, f'DROP SCHEMA "{self.schema}" CASCADE')

    def test_postgres_rows_and_placeholders(self):
        db = open_database(PG_URL, schema=self.schema)
        self.addCleanup(db.close)
        self.assertEqual(db.dialect, "postgres")
        with db.transaction():
            db.execute("CREATE TABLE t(a TEXT)")
            db.execute("INSERT INTO t(a) VALUES(?)", ("x",))
            db.execute("INSERT INTO t(a) VALUES('what?')")
            db.execute("INSERT INTO t(a) VALUES(?)", ("50% done?",))
        rows = db.execute("SELECT a FROM t WHERE a=? OR a LIKE '%?'", ("x",)).fetchall()
        self.assertEqual(sorted(r["a"] for r in rows), ["50% done?", "what?", "x"])
        row = db.execute("SELECT a FROM t WHERE a=?", ("x",)).fetchone()
        self.assertEqual(dict(row), {"a": "x"})

    def test_postgres_reconnects_after_dropped_connection(self):
        db = open_database(PG_URL, schema=self.schema)
        self.addCleanup(db.close)
        pid = db.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        self.admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        self.assertEqual(db.execute("SELECT 1 AS one").fetchone()["one"], 1)
