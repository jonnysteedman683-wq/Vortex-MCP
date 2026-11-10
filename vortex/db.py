"""Database layer under VortexStore: one interface, a SQLite and a Postgres backend."""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

POSTGRES_PREFIXES = ("postgresql://", "postgres://")


def schema_sql(name: str) -> str:
    """Return the text of ``vortex/schema/<name>``."""
    return resources.files("vortex").joinpath("schema", name).read_text(encoding="utf-8")


class Cursor(Protocol):
    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...


class Database(Protocol):
    """What VortexStore needs from a database. Rows behave like mappings."""

    dialect: Literal["sqlite", "postgres"]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor: ...

    def transaction(self) -> AbstractContextManager[Any]: ...

    def prepare(self) -> None:
        """Make the schema ready: SQLite migrates in place; Postgres checks it was migrated."""

    def close(self) -> None: ...


class SqliteDatabase:
    """SQLite backend: today's behaviour, unchanged."""

    dialect: Literal["sqlite"] = "sqlite"

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    def transaction(self) -> AbstractContextManager[Any]:
        # sqlite3's connection context manager commits on success and rolls back on error.
        return self._conn

    def columns(self, table: str) -> set[str]:
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def prepare(self) -> None:
        self.migrate()

    def migrate(self) -> None:
        """Create tables and apply the additive column migrations older databases need."""
        with self._conn:
            self._conn.executescript(schema_sql("sqlite.sql"))
            self._conn.execute("DROP INDEX IF EXISTS idx_messages_inbox")
            if "task_id" not in self.columns("messages"):
                self._conn.execute("ALTER TABLE messages ADD COLUMN task_id TEXT REFERENCES tasks(task_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id)")
            if "approval_state" not in self.columns("tasks"):
                # Additive migration: existing tasks stay claimable ('not_required').
                self._conn.execute(
                    """
                    ALTER TABLE tasks ADD COLUMN approval_state TEXT NOT NULL DEFAULT 'not_required'
                        CHECK(approval_state IN ('not_required','pending','approved','rejected'))
                    """
                )
            self._conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_tasks_pending_approval
                ON tasks(status, approval_state, priority, created_at, task_id)
                """
            )
            self._conn.execute("DROP INDEX IF EXISTS idx_tasks_approval")
            agent_columns = self.columns("agents")
            # Additive migration for the per-agent kill switch.
            if "paused_at" not in agent_columns:
                self._conn.execute("ALTER TABLE agents ADD COLUMN paused_at TEXT")
            if "paused_reason" not in agent_columns:
                self._conn.execute("ALTER TABLE agents ADD COLUMN paused_reason TEXT")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operator_actions (
                    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor_id TEXT NOT NULL REFERENCES agents(agent_id),
                    action TEXT NOT NULL,
                    target_id TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def close(self) -> None:
        self._conn.close()

    # Existing SQLite-specific tests reach the raw connection through the store; keep that working.
    def __enter__(self) -> sqlite3.Connection:
        return self._conn.__enter__()

    def __exit__(self, *exc: object) -> Any:
        return self._conn.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def open_database(target: str, *, schema: str = "forge") -> Database:
    """A ``postgresql://`` URL opens Postgres; anything else is a SQLite path (or ``:memory:``)."""
    if target.startswith(POSTGRES_PREFIXES):
        raise NotImplementedError("Postgres backend is not available yet")
    return SqliteDatabase(target)
