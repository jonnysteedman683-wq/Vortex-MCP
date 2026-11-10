"""Database layer under VortexStore: one interface, a SQLite and a Postgres backend."""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from importlib import resources
from pathlib import Path
from typing import Any, Literal, Protocol

POSTGRES_PREFIXES = ("postgresql://", "postgres://")


def schema_sql(name: str) -> str:
    """Return the text of ``vortex/schema/<name>``."""
    return resources.files("vortex").joinpath("schema", name).read_text(encoding="utf-8")


def check_params(params: Sequence[Any]) -> tuple[Any, ...]:
    """Postgres TEXT cannot hold NUL. Reject it for every parameter on every backend, as a ValueError."""
    values = tuple(params)
    for value in values:
        if isinstance(value, str) and "\x00" in value:
            raise ValueError("text must not contain NUL characters")
    return values


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
        return self._conn.execute(sql, check_params(params))

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


STATEMENT_TIMEOUT = "30s"
SCHEMA_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


def check_schema_name(schema: str) -> str:
    if not isinstance(schema, str) or not SCHEMA_NAME.fullmatch(schema):
        raise ValueError("schema name must match ^[a-z_][a-z0-9_]{0,62}$")
    return schema


def to_pyformat(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s`` outside quoted literals and escape literal ``%``."""
    out: list[str] = []
    quote: str | None = None
    comment: str | None = None  # "line" for -- ... newline, "block" for /* ... */
    i = 0
    while i < len(sql):
        ch = sql[i]
        if comment == "line":
            if ch == "\n":
                comment = None
        elif comment == "block":
            if sql.startswith("*/", i):
                out.append("*/")
                i += 2
                comment = None
                continue
        elif quote:
            if ch == quote:
                quote = None
        elif sql.startswith("--", i):
            comment = "line"
        elif sql.startswith("/*", i):
            comment = "block"
        elif ch in ("'", '"'):
            quote = ch
        elif ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append("%%" if ch == "%" else ch)
        i += 1
    return "".join(out)


class PostgresDatabase:
    """Postgres backend. Use a direct or session-mode connection: the search path (and the instance
    lock) live on the session, which a transaction-mode pooler would drop between statements."""

    dialect: Literal["postgres"] = "postgres"

    def __init__(self, url: str, *, schema: str):
        import psycopg  # imported here so SQLite-only installs never need it

        self._psycopg = psycopg
        self._url = url
        self.schema = check_schema_name(schema)
        self._depth = 0
        self._locked = False
        self._lock_lost = False
        self._conn = self._connect()

    def _connect(self) -> Any:
        from psycopg.rows import dict_row

        try:
            conn = self._psycopg.connect(
                self._url, autocommit=True, row_factory=dict_row, connect_timeout=10,
                # Detect a silently dropped connection (NAT idle timeout) instead of blocking forever.
                keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
            )
        except self._psycopg.Error:
            # Never chain or echo the driver's message: for a malformed URL it quotes parts of it.
            raise ConnectionError("database unavailable") from None
        conn.execute(f'SET search_path TO "{self.schema}"')
        conn.execute(f"SET statement_timeout = '{STATEMENT_TIMEOUT}'")
        return conn

    def _reconnect(self) -> None:
        with suppress(Exception):  # the old connection is already broken
            self._conn.close()
        self._conn = self._connect()
        if self._locked and not self._try_lock():
            # Another server took over while we were disconnected: stop writing for good.
            self._lock_lost = True

    def _try_lock(self, name: str = "vortex") -> bool:
        row = self._conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (f"{name}:{self.schema}",)
        ).fetchone()
        return bool(row["ok"])

    def acquire_relay_lock(self) -> None:
        """Hold a separate session lock so only one relay copies this schema's outbox."""
        if not self._try_lock("vortex-relay"):
            raise RuntimeError(f"another Vortex relay is already running for schema {self.schema}")

    def _lost(self) -> RuntimeError:
        return RuntimeError(f"another Vortex server is already using schema {self.schema}")

    def acquire_instance_lock(self) -> None:
        """Hold a session advisory lock so only one Vortex server uses this schema."""
        if not self._try_lock():
            raise self._lost()
        self._locked = True

    def execute(self, sql: str, params: Sequence[Any] = ()) -> Any:
        if self._lock_lost:
            raise self._lost()
        params = check_params(params)
        query = to_pyformat(sql)
        try:
            return self._conn.execute(query, tuple(params))
        except self._psycopg.OperationalError:
            if self._depth:
                # Inside a transaction a retry would silently drop the earlier statements.
                raise ConnectionError("database unavailable") from None
            self._reconnect()
            if self._lock_lost:
                raise self._lost() from None
            try:
                return self._conn.execute(query, tuple(params))
            except self._psycopg.OperationalError:
                raise ConnectionError("database unavailable") from None

    def _begin(self) -> Any:
        tx = self._conn.transaction()
        tx.__enter__()
        return tx

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._lock_lost:
            raise self._lost()
        if self._depth == 0 and (self._conn.closed or self._conn.broken):
            self._reconnect()
            if self._lock_lost:
                raise self._lost()
        try:
            tx = self._begin()
        except self._psycopg.OperationalError:
            if self._depth:
                raise ConnectionError("database unavailable") from None
            # Nothing has been written yet, so reconnecting and retrying BEGIN once is safe.
            self._reconnect()
            if self._lock_lost:
                raise self._lost() from None
            try:
                tx = self._begin()
            except self._psycopg.OperationalError:
                raise ConnectionError("database unavailable") from None
        self._depth += 1
        try:
            yield
        except BaseException as exc:
            self._depth -= 1
            tx.__exit__(type(exc), exc, exc.__traceback__)
            raise
        self._depth -= 1
        tx.__exit__(None, None, None)

    def prepare(self) -> None:
        """Schema creation belongs to ``python -m vortex migrate``; at runtime only check it happened."""
        for table in ("tasks", "agent_keys"):  # the first table, and the most recently added one
            found = self.execute("SELECT to_regclass(?) AS t", (f'"{self.schema}".{table}',)).fetchone()
            if found is None or found["t"] is None:
                raise RuntimeError(
                    f"schema {self.schema} is missing or out of date; run: python -m vortex migrate"
                )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> None:
        self._tx = self.transaction()
        return self._tx.__enter__()

    def __exit__(self, *exc: object) -> Any:
        return self._tx.__exit__(*exc)


def open_database(target: str, *, schema: str = "forge") -> Database:
    """A ``postgresql://`` URL opens Postgres; anything else is a SQLite path (or ``:memory:``)."""
    if target.startswith(POSTGRES_PREFIXES):
        return PostgresDatabase(target, schema=schema)
    return SqliteDatabase(target)
