"""SQLite persistence for Vortex agent presence and messages."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .settings import DEFAULT_AGENT_IDS

_DEFAULT_AGENT_LABELS = {
    "hermes-1": "Hermes instance 1",
    "hermes-2": "Hermes instance 2",
    "antigravity-1": "Antigravity instance 1",
    "spark-1": "Gemini Spark instance 1",
}
DEFAULT_AGENTS = {
    agent_id: _DEFAULT_AGENT_LABELS.get(agent_id, agent_id.replace("-", " ").title())
    for agent_id in DEFAULT_AGENT_IDS
}
MAX_MESSAGE_CHARS = 8000
MAX_TASK_GOAL_CHARS = 4000
MAX_TASK_RESULT_CHARS = 8000
MAX_TASK_DEPENDENCIES = 20
DEFAULT_TASK_LEASE_SECONDS = 300
MAX_TASK_LEASE_SECONDS = 3600


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


class VortexStore:
    """Synchronous, thread-safe SQLite store; no message body is written to logs."""

    def __init__(self, path: str | Path, agents: dict[str, str] | None = None):
        self.path = str(path)
        self._agents = dict(DEFAULT_AGENTS)
        if agents:
            self._agents.update(agents)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self._db.execute("PRAGMA journal_mode = WAL")
        self._initialize()

    def _initialize(self) -> None:
        with self._lock, self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'offline',
                    last_seen TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    message_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    sender_id TEXT NOT NULL REFERENCES agents(agent_id),
                    recipient_id TEXT NOT NULL REFERENCES agents(agent_id),
                    body TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    acked_at TEXT,
                    reply_to TEXT REFERENCES messages(message_id),
                    task_id TEXT REFERENCES tasks(task_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(recipient_id, acked_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_inbox_cursor
                    ON messages(recipient_id, acked_at, created_at, message_id);
                CREATE INDEX IF NOT EXISTS idx_messages_thread
                    ON messages(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_reply_to
                    ON messages(reply_to);
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    goal TEXT NOT NULL,
                    created_by TEXT NOT NULL REFERENCES agents(agent_id),
                    assigned_to TEXT REFERENCES agents(agent_id),
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','in_progress','blocked','completed','cancelled')),
                    priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    lease_expires_at TEXT,
                    blocked_reason TEXT,
                    result TEXT,
                    idempotency_key TEXT,
                    UNIQUE(created_by, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS task_dependencies (
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    depends_on TEXT NOT NULL REFERENCES tasks(task_id),
                    PRIMARY KEY(task_id, depends_on),
                    CHECK(task_id != depends_on)
                );
                CREATE TABLE IF NOT EXISTS task_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    actor_id TEXT NOT NULL REFERENCES agents(agent_id),
                    event_type TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_creator_status
                    ON tasks(created_by, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status
                    ON tasks(assigned_to, status, updated_at);
                CREATE INDEX IF NOT EXISTS idx_task_dependencies_parent
                    ON task_dependencies(depends_on);
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, event_id);
            """)
            message_columns = {
                row["name"] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()
            }
            if "task_id" not in message_columns:
                self._db.execute(
                    "ALTER TABLE messages ADD COLUMN task_id TEXT REFERENCES tasks(task_id)"
                )
            self._db.execute("CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id)")
            for agent_id, display_name in self._agents.items():
                platform = "hermes" if agent_id.startswith("hermes-") else (
                    "antigravity" if agent_id.startswith("antigravity-") else "spark"
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO agents(agent_id,display_name,platform,created_at) VALUES(?,?,?,?)",
                    (agent_id, display_name, platform, iso(utc_now())),
                )

    def _task_access(self, agent_id: str, task_id: str) -> sqlite3.Row:
        if self._get_agent(self._db, agent_id) is None:
            raise ValueError("Unknown agent identity")
        row = self._db.execute(
            """
            SELECT * FROM tasks
            WHERE task_id=?
              AND (created_by=? OR assigned_to=? OR (assigned_to IS NULL AND status='open'))
            """,
            (task_id, agent_id, agent_id),
        ).fetchone()
        if row is None:
            exists = self._db.execute(
                "SELECT 1 FROM tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if exists is None:
                raise ValueError("Task not found")
            raise PermissionError("Only the task creator or assignee can access this task")
        return row

    def _task_view(self, row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["depends_on"] = [
            dependency["depends_on"]
            for dependency in self._db.execute(
                "SELECT depends_on FROM task_dependencies WHERE task_id=? ORDER BY depends_on",
                (task["task_id"],),
            ).fetchall()
        ]
        task["blocked_by"] = [
            dependency["depends_on"]
            for dependency in self._db.execute(
                """
                SELECT d.depends_on
                FROM task_dependencies d
                JOIN tasks parent ON parent.task_id=d.depends_on
                WHERE d.task_id=? AND parent.status != 'completed'
                ORDER BY d.depends_on
                """,
                (task["task_id"],),
            ).fetchall()
        ]
        task["ready"] = task["status"] == "open" and not task["blocked_by"]
        return task

    def _task_event(
        self,
        task_id: str,
        actor_id: str,
        event_type: str,
        details: dict[str, Any],
        created_at: str,
    ) -> None:
        self._db.execute(
            "INSERT INTO task_events(task_id,actor_id,event_type,details_json,created_at) VALUES(?,?,?,?,?)",
            (task_id, actor_id, event_type, json.dumps(details, sort_keys=True), created_at),
        )

    def create_task(
        self,
        creator_id: str,
        goal: str,
        assigned_to: str | None = None,
        priority: int = 3,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(goal, str):
            raise ValueError("Task goal must be a string")
        goal = goal.strip()
        if not goal:
            raise ValueError("Task goal must not be empty")
        if len(goal) > MAX_TASK_GOAL_CHARS:
            raise ValueError(f"Task goal exceeds {MAX_TASK_GOAL_CHARS} characters")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ValueError("Priority must be an integer from 1 (highest) to 5 (lowest)")
        if assigned_to is not None and (not isinstance(assigned_to, str) or not assigned_to):
            raise ValueError("Task assignee must be a configured agent ID")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200
        ):
            raise ValueError("Idempotency key must be a non-empty string of at most 200 characters")
        if idempotency_key is not None:
            idempotency_key = idempotency_key.strip()
        dependencies = depends_on or []
        if not isinstance(dependencies, list) or len(dependencies) > MAX_TASK_DEPENDENCIES:
            raise ValueError(f"Provide no more than {MAX_TASK_DEPENDENCIES} task dependencies")
        if any(not isinstance(task_id, str) or not task_id for task_id in dependencies):
            raise ValueError("Dependency task IDs must be non-empty strings")
        if len(set(dependencies)) != len(dependencies):
            raise ValueError("Dependency task IDs must not contain duplicates")

        with self._lock, self._db:
            if self._get_agent(self._db, creator_id) is None:
                raise ValueError("Unknown task creator")
            if assigned_to is not None and self._get_agent(self._db, assigned_to) is None:
                raise ValueError("Unknown task assignee")
            for dependency_id in dependencies:
                self._task_access(creator_id, dependency_id)
            if idempotency_key:
                existing = self._db.execute(
                    "SELECT * FROM tasks WHERE created_by=? AND idempotency_key=?",
                    (creator_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    existing_view = self._task_view(existing)
                    if (
                        existing["goal"] != goal
                        or existing["assigned_to"] != assigned_to
                        or existing["priority"] != priority
                        or existing_view["depends_on"] != sorted(dependencies)
                    ):
                        raise ValueError("Idempotency key was already used for a different task")
                    return existing_view

            task_id = str(uuid.uuid4())
            created_at = iso(utc_now())
            cursor = self._db.execute(
                """
                INSERT OR IGNORE INTO tasks(
                    task_id,goal,created_by,assigned_to,priority,created_at,updated_at,idempotency_key
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (task_id, goal, creator_id, assigned_to, priority, created_at, created_at, idempotency_key),
            )
            if cursor.rowcount != 1:
                existing = self._db.execute(
                    "SELECT * FROM tasks WHERE created_by=? AND idempotency_key=?",
                    (creator_id, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("Task insert was ignored by an unexpected database constraint")
                existing_view = self._task_view(existing)
                if (
                    existing["goal"] != goal
                    or existing["assigned_to"] != assigned_to
                    or existing["priority"] != priority
                    or existing_view["depends_on"] != sorted(dependencies)
                ):
                    raise ValueError("Idempotency key was already used for a different task")
                return existing_view
            for dependency_id in dependencies:
                self._db.execute(
                    "INSERT INTO task_dependencies(task_id,depends_on) VALUES(?,?)",
                    (task_id, dependency_id),
                )
            self._task_event(
                task_id,
                creator_id,
                "created",
                {"assigned_to": assigned_to, "depends_on": sorted(dependencies), "priority": priority},
                created_at,
            )
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def dispatch_tasks(
        self,
        creator_id: str,
        goal: str,
        recipients: list[str],
        priority: int = 3,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(recipients, list) or not recipients:
            raise ValueError("Provide at least one recipient for mass dispatch")
        if len(recipients) > 100:
            raise ValueError("Provide no more than 100 recipients in one mass dispatch")
        if any(not isinstance(recipient, str) or not recipient.strip() for recipient in recipients):
            raise ValueError("Recipients must be non-empty agent IDs")
        if len(set(recipients)) != len(recipients):
            raise ValueError("Recipients must not contain duplicates")

        dispatched: list[dict[str, Any]] = []
        base_key = idempotency_key.strip() if isinstance(idempotency_key, str) and idempotency_key.strip() else None
        effective_keys = {
            recipient: f"{base_key}:{recipient}" if base_key else None
            for recipient in recipients
        }

        with self._lock, self._db:
            if self._get_agent(self._db, creator_id) is None:
                raise ValueError("Unknown task creator")
            for recipient in recipients:
                if self._get_agent(self._db, recipient) is None:
                    raise ValueError(f"Unknown recipient agent identity: {recipient}")
            for dependency_id in depends_on or []:
                self._task_access(creator_id, dependency_id)
            for recipient in recipients:
                dispatched.append(
                    self.create_task(
                        creator_id,
                        goal,
                        assigned_to=recipient,
                        priority=priority,
                        depends_on=depends_on,
                        idempotency_key=effective_keys[recipient],
                    )
                )
        return dispatched

    def list_tasks(
        self,
        agent_id: str,
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if status is not None and status not in {"open", "in_progress", "blocked", "completed", "cancelled"}:
            raise ValueError("Unknown task status")
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if status is not None:
            query += " AND status=?"
            args.append(status)
        query += " AND (created_by=? OR assigned_to=? OR (assigned_to IS NULL AND status='open'))"
        args.extend((agent_id, agent_id))
        query += " ORDER BY priority,created_at,task_id LIMIT ?"
        args.append(limit)
        with self._lock:
            if self._get_agent(self._db, agent_id) is None:
                raise ValueError("Unknown agent identity")
            rows = self._db.execute(query, args).fetchall()
            return [self._task_view(row) for row in rows]

    def get_task(self, agent_id: str, task_id: str) -> dict[str, Any]:
        with self._lock:
            return self._task_view(self._task_access(agent_id, task_id))

    def task_events(self, agent_id: str, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._task_access(agent_id, task_id)
            rows = self._db.execute(
                "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)
            ).fetchall()
            events = [dict(row) for row in rows]
        for event in events:
            event["details"] = json.loads(event.pop("details_json"))
        return events

    def claim_task(
        self,
        agent_id: str,
        task_id: str,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict[str, Any]:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise ValueError("Lease duration must be an integer")
        if not 30 <= lease_seconds <= MAX_TASK_LEASE_SECONDS:
            raise ValueError(f"Lease duration must be between 30 and {MAX_TASK_LEASE_SECONDS} seconds")
        with self._lock, self._db:
            if self._get_agent(self._db, agent_id) is None:
                raise ValueError("Unknown agent identity")
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            if row["status"] not in {"open", "in_progress", "blocked"}:
                raise ValueError(f"Task cannot be claimed while status is {row['status']}")
            now = utc_now()
            now_text = iso(now)
            if row["assigned_to"] not in (None, agent_id) and (
                row["status"] != "in_progress"
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] > now_text
            ):
                raise PermissionError("Task is assigned to another agent with an active lease")
            blocked_by = self._db.execute(
                """
                SELECT d.depends_on FROM task_dependencies d
                JOIN tasks parent ON parent.task_id=d.depends_on
                WHERE d.task_id=? AND parent.status != 'completed' LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if blocked_by:
                raise ValueError(f"Task is waiting for dependency {blocked_by['depends_on']}")
            expires_at = iso(now + timedelta(seconds=lease_seconds))
            cursor = self._db.execute(
                """
                UPDATE tasks SET assigned_to=?,status='in_progress',lease_expires_at=?,updated_at=?,blocked_reason=NULL
                WHERE task_id=? AND status IN ('open','in_progress','blocked')
                  AND (assigned_to IS NULL OR assigned_to=? OR
                       (status='in_progress' AND lease_expires_at <= ?))
                  AND (status='open' OR assigned_to=? OR lease_expires_at <= ?)
                  AND NOT EXISTS (
                    SELECT 1 FROM task_dependencies d JOIN tasks parent ON parent.task_id=d.depends_on
                    WHERE d.task_id=tasks.task_id AND parent.status != 'completed'
                  )
                """,
                (agent_id, expires_at, now_text, task_id, agent_id, now_text, agent_id, now_text),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task claim lost a race or the previous lease is still active")
            self._task_event(
                task_id,
                agent_id,
                "claimed",
                {
                    "lease_expires_at": expires_at,
                    "reclaimed_from": row["assigned_to"] if row["assigned_to"] != agent_id else None,
                },
                now_text,
            )
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def update_task(
        self,
        agent_id: str,
        task_id: str,
        status: str,
        blocked_reason: str | None = None,
        lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS,
    ) -> dict[str, Any]:
        if status not in {"in_progress", "blocked"}:
            raise ValueError("Task status updates must be 'in_progress' or 'blocked'")
        if status == "blocked" and (
            not isinstance(blocked_reason, str) or not blocked_reason.strip()
        ):
            raise ValueError("A blocked task requires a non-empty reason")
        if blocked_reason is not None and (
            not isinstance(blocked_reason, str) or len(blocked_reason) > 1000
        ):
            raise ValueError("Blocked reason exceeds 1000 characters")
        if status == "in_progress" and (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or not 30 <= lease_seconds <= MAX_TASK_LEASE_SECONDS
        ):
            raise ValueError(f"Lease duration must be an integer between 30 and {MAX_TASK_LEASE_SECONDS} seconds")

        with self._lock, self._db:
            row = self._task_access(agent_id, task_id)
            if row["assigned_to"] != agent_id:
                raise PermissionError("Only the assigned agent can update task progress")
            if row["status"] != "in_progress":
                raise ValueError(f"Task cannot be updated while status is {row['status']}")
            now = utc_now()
            now_text = iso(now)
            if row["lease_expires_at"] is None or row["lease_expires_at"] <= now_text:
                raise ValueError("Task lease expired; claim the task again before updating it")
            lease_expires_at = (
                iso(now + timedelta(seconds=lease_seconds)) if status == "in_progress" else None
            )
            cursor = self._db.execute(
                """
                UPDATE tasks SET status=?,blocked_reason=?,lease_expires_at=?,updated_at=?
                WHERE task_id=? AND assigned_to=? AND status='in_progress' AND lease_expires_at > ?
                """,
                (
                    status,
                    blocked_reason.strip() if blocked_reason else None,
                    lease_expires_at,
                    now_text,
                    task_id,
                    agent_id,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task lease expired or ownership changed before the update")
            details = {"blocked_reason": blocked_reason.strip()} if status == "blocked" else {
                "lease_expires_at": lease_expires_at
            }
            self._task_event(task_id, agent_id, status, details, now_text)
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def complete_task(self, agent_id: str, task_id: str, result: str = "") -> dict[str, Any]:
        if not isinstance(result, str) or len(result) > MAX_TASK_RESULT_CHARS:
            raise ValueError(f"Task result must be a string of at most {MAX_TASK_RESULT_CHARS} characters")
        with self._lock, self._db:
            row = self._task_access(agent_id, task_id)
            if row["assigned_to"] != agent_id:
                raise PermissionError("Only the assigned agent can complete this task")
            if row["status"] != "in_progress":
                raise ValueError("Only an in-progress task can be completed")
            now_text = iso(utc_now())
            if row["lease_expires_at"] is None or row["lease_expires_at"] <= now_text:
                raise ValueError("Task lease expired; claim the task again before completing it")
            cursor = self._db.execute(
                """
                UPDATE tasks SET status='completed',result=?,lease_expires_at=NULL,updated_at=?
                WHERE task_id=? AND assigned_to=? AND status='in_progress' AND lease_expires_at > ?
                """,
                (result, now_text, task_id, agent_id, now_text),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task lease expired or ownership changed before completion")
            self._task_event(task_id, agent_id, "completed", {"result": result}, now_text)
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def cancel_task(self, agent_id: str, task_id: str, reason: str = "") -> dict[str, Any]:
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("Cancellation reason must be a string of at most 1000 characters")
        with self._lock, self._db:
            row = self._task_access(agent_id, task_id)
            if row["status"] in {"completed", "cancelled"}:
                raise ValueError(f"Task is already {row['status']}")
            now_text = iso(utc_now())
            cursor = self._db.execute(
                """
                UPDATE tasks SET status='cancelled',lease_expires_at=NULL,updated_at=?
                WHERE task_id=? AND status NOT IN ('completed','cancelled')
                  AND (created_by=? OR assigned_to=?)
                """,
                (now_text, task_id, agent_id, agent_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task became terminal before cancellation")
            self._task_event(
                task_id, agent_id, "cancelled", {"reason": reason.strip()}, now_text
            )
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def register(self, agent_id: str) -> dict[str, Any]:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown agent identity; use a configured Vortex agent ID")
            now = iso(utc_now())
            self._db.execute("UPDATE agents SET status='online',last_seen=? WHERE agent_id=?", (now, agent_id))
            result = dict(row)
            result.update(status="online", last_seen=now)
            return result

    def list_agents(self, current_agent: str, stale_after_seconds: int = 90) -> list[dict[str, Any]]:
        cutoff = utc_now() - timedelta(seconds=stale_after_seconds)
        with self._lock:
            rows = self._db.execute("SELECT * FROM agents ORDER BY agent_id").fetchall()
        agents = []
        for row in rows:
            entry = dict(row)
            seen = datetime.fromisoformat(entry["last_seen"]) if entry["last_seen"] else None
            entry["status"] = "online" if seen and seen >= cutoff else "offline"
            entry["is_self"] = entry["agent_id"] == current_agent
            agents.append(entry)
        return agents

    def _get_agent(self, conn: sqlite3.Connection, agent_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT agent_id FROM agents WHERE agent_id=?", (agent_id,)).fetchone()

    def send(self, sender_id: str, recipient_id: str, body: str,
             thread_id: str | None = None, reply_to: str | None = None,
             task_id: str | None = None) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ValueError("Message body must not be empty")
        if len(body) > MAX_MESSAGE_CHARS:
            raise ValueError(f"Message body exceeds {MAX_MESSAGE_CHARS} characters")
        if sender_id == recipient_id:
            raise ValueError("Cannot send a message to the same agent identity")
        with self._lock, self._db:
            sender = self._get_agent(self._db, sender_id)
            recipient = self._get_agent(self._db, recipient_id)
            if sender is None:
                raise ValueError("Unknown sender identity")
            if recipient is None:
                raise ValueError("Unknown recipient identity")
            if task_id is not None:
                self._task_access(sender_id, task_id)
                self._task_access(recipient_id, task_id)
            if thread_id and not reply_to:
                raise ValueError("Use reply_to to add a message to an existing thread")
            resolved_thread_id = thread_id
            if reply_to:
                parent = self._db.execute(
                    "SELECT thread_id,sender_id,recipient_id,task_id FROM messages WHERE message_id=?",
                    (reply_to,),
                ).fetchone()
                if parent is None:
                    raise ValueError("Reply target does not exist")
                if sender_id not in (parent["sender_id"], parent["recipient_id"]):
                    raise PermissionError("Only participants in a message thread may reply")
                if recipient_id not in (parent["sender_id"], parent["recipient_id"]) or recipient_id == sender_id:
                    raise PermissionError("Reply must be sent to the other participant")
                if resolved_thread_id and resolved_thread_id != parent["thread_id"]:
                    raise ValueError("reply_to and thread_id identify different threads")
                resolved_thread_id = parent["thread_id"]
                if task_id is not None and parent["task_id"] != task_id:
                    raise ValueError("Reply target belongs to a different task")
                task_id = parent["task_id"]
            message_id = str(uuid.uuid4())
            resolved_thread_id = resolved_thread_id or str(uuid.uuid4())
            created_at = iso(utc_now())
            self._db.execute(
                """
                INSERT INTO messages(message_id,thread_id,sender_id,recipient_id,body,created_at,reply_to,task_id)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (message_id, resolved_thread_id, sender_id, recipient_id, body, created_at, reply_to, task_id),
            )
        return {"message_id": message_id, "thread_id": resolved_thread_id, "sender_id": sender_id,
                "recipient_id": recipient_id, "body": body, "created_at": created_at,
                "reply_to": reply_to, "task_id": task_id, "delivery": "queued"}

    def inbox(self, agent_id: str, limit: int = 20, include_acked: bool = False,
              after_message_id: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM messages WHERE recipient_id=?"
        args: list[Any] = [agent_id]
        if not include_acked:
            query += " AND acked_at IS NULL"
        with self._lock:
            if after_message_id is not None:
                cursor = self._db.execute(
                    "SELECT recipient_id,created_at,message_id FROM messages WHERE message_id=?",
                    (after_message_id,),
                ).fetchone()
                if cursor is None:
                    raise ValueError("Inbox cursor message not found")
                if cursor["recipient_id"] != agent_id:
                    raise PermissionError("Inbox cursor must belong to the authenticated recipient")
                query += " AND (created_at > ? OR (created_at = ? AND message_id > ?))"
                args.extend((cursor["created_at"], cursor["created_at"], cursor["message_id"]))
            query += " ORDER BY created_at,message_id LIMIT ?"
            args.append(limit)
            rows = self._db.execute(query, args).fetchall()
        return [dict(row) for row in rows]

    def acknowledge_many(self, agent_id: str, message_ids: list[str]) -> list[dict[str, Any]]:
        if not 1 <= len(message_ids) <= 100:
            raise ValueError("Provide between 1 and 100 message IDs")
        if any(not isinstance(message_id, str) or not message_id for message_id in message_ids):
            raise ValueError("Message IDs must be non-empty strings")
        if len(set(message_ids)) != len(message_ids):
            raise ValueError("Message IDs must not contain duplicates")

        with self._lock, self._db:
            rows = []
            for message_id in message_ids:
                row = self._db.execute(
                    "SELECT * FROM messages WHERE message_id=?", (message_id,)
                ).fetchone()
                if row is None:
                    raise ValueError("Message not found")
                if row["recipient_id"] != agent_id:
                    raise PermissionError("Only the addressed recipient can acknowledge a message")
                rows.append(row)

            acked_at = iso(utc_now())
            for row in rows:
                if row["acked_at"] is None:
                    self._db.execute(
                        "UPDATE messages SET acked_at=? WHERE message_id=?",
                        (acked_at, row["message_id"]),
                    )

            acknowledged = []
            for message_id in message_ids:
                row = self._db.execute(
                    "SELECT * FROM messages WHERE message_id=?", (message_id,)
                ).fetchone()
                acknowledged.append(dict(row))
        return acknowledged

    def acknowledge(self, agent_id: str, message_id: str) -> dict[str, Any]:
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if row is None:
                raise ValueError("Message not found")
            if row["recipient_id"] != agent_id:
                raise PermissionError("Only the addressed recipient can acknowledge this message")
            acked_at = row["acked_at"] or iso(utc_now())
            if row["acked_at"] is None:
                self._db.execute("UPDATE messages SET acked_at=? WHERE message_id=?", (acked_at, message_id))
            result = dict(row)
            result["acked_at"] = acked_at
            return result

    def thread(self, agent_id: str, message_id: str) -> list[dict[str, Any]]:
        with self._lock:
            message = self._db.execute("SELECT thread_id,sender_id,recipient_id FROM messages WHERE message_id=?", (message_id,)).fetchone()
            if message is None:
                raise ValueError("Message not found")
            if agent_id not in (message["sender_id"], message["recipient_id"]):
                raise PermissionError("Only participants can read this thread")
            rows = self._db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (message["thread_id"],)).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
