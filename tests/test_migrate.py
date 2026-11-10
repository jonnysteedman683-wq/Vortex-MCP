"""Tests for `python -m vortex migrate` and the vortex_writer role (Postgres only)."""
import unittest
import uuid

from tests.backends import PG_URL, drop_schema
from vortex.migrate import apply
from vortex.store import VortexStore


@unittest.skipUnless(PG_URL, "needs VORTEX_TEST_DATABASE_URL")
class MigrateTests(unittest.TestCase):
    def setUp(self):
        import psycopg

        self.psycopg = psycopg
        self.schema = "t_" + uuid.uuid4().hex
        self.addCleanup(drop_schema, PG_URL, self.schema)

    def _tables(self):
        with self.psycopg.connect(PG_URL) as conn:
            rows = conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema=%s", (self.schema,)
            ).fetchall()
        return {r[0] for r in rows}

    def test_apply_is_idempotent(self):
        apply(PG_URL, self.schema)
        apply(PG_URL, self.schema)
        self.assertTrue(
            {"agents", "tasks", "messages", "task_events", "operator_actions", "agent_keys"} <= self._tables()
        )

    def test_store_refuses_a_schema_migrated_before_agent_keys(self):
        apply(PG_URL, self.schema)
        with self.psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f'DROP TABLE "{self.schema}".agent_keys')
        with self.assertRaisesRegex(RuntimeError, "python -m vortex migrate"):
            VortexStore(PG_URL, schema=self.schema)
        apply(PG_URL, self.schema)  # re-running migrate adds the table without touching the rest
        store = VortexStore(PG_URL, schema=self.schema)
        self.addCleanup(store.close)
        self.assertEqual(store.agent_for_key(store.issue_key("hermes-1")), "hermes-1")

    def test_writer_can_issue_look_up_and_revoke_keys(self):
        apply(PG_URL, self.schema)
        role = "vw_" + uuid.uuid4().hex[:12]
        with self.psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {role} LOGIN IN ROLE vortex_writer")
        self.addCleanup(self._drop_role, role)
        store = VortexStore(PG_URL.replace("postgres@", f"{role}@", 1), schema=self.schema)
        self.addCleanup(store.close)
        key = store.issue_key("hermes-1")
        self.assertEqual(store.agent_for_key(key), "hermes-1")
        self.assertEqual(store.revoke_keys("hermes-1"), 1)
        self.assertIsNone(store.agent_for_key(key))

    def test_agent_keys_text_columns_use_c_collation(self):
        apply(PG_URL, self.schema)
        with self.psycopg.connect(PG_URL) as conn:
            rows = conn.execute(
                "SELECT column_name, collation_name FROM information_schema.columns"
                " WHERE table_schema=%s AND table_name='agent_keys'", (self.schema,)
            ).fetchall()
        self.assertEqual({r[0] for r in rows}, {"key_hash", "agent_id", "created_at", "revoked_at"})
        self.assertEqual({r[1] for r in rows}, {"C"})

    def test_writer_cannot_run_ddl(self):
        apply(PG_URL, self.schema)
        role = "vw_" + uuid.uuid4().hex[:12]
        with self.psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"CREATE ROLE {role} LOGIN IN ROLE vortex_writer")
        self.addCleanup(self._drop_role, role)
        writer_url = PG_URL.replace("postgres@", f"{role}@", 1)
        with self.psycopg.connect(writer_url, autocommit=True) as conn:
            conn.execute(f'SET search_path TO "{self.schema}"')
            conn.execute(
                "INSERT INTO agents(agent_id,display_name,platform,created_at) VALUES('a','A','spark','t')"
            )
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("CREATE TABLE x(a int)")

    def _drop_role(self, role):
        with self.psycopg.connect(PG_URL, autocommit=True) as admin:
            admin.execute(f"DROP OWNED BY {role}")
            admin.execute(f"DROP ROLE {role}")

    def test_store_refuses_unmigrated_schema(self):
        with self.assertRaisesRegex(RuntimeError, "python -m vortex migrate"):
            VortexStore(PG_URL, schema="nope")

    def test_bad_schema_name_is_refused(self):
        with self.assertRaises(ValueError):
            apply(PG_URL, "x; drop")
