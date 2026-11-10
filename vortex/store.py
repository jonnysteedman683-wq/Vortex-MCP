"""SQLite persistence for Vortex agent presence and messages."""
from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database, open_database
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
    return datetime.now(UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


TASK_STATUSES = ("open", "in_progress", "blocked", "completed", "cancelled")
CLAIMABLE_APPROVAL_STATES = ("not_required", "approved")
MAX_APPROVAL_NOTE_CHARS = 1000


def _check_text(name: str, value: str) -> None:
    """Postgres TEXT cannot hold NUL; reject it on every backend so both behave the same."""
    if "\x00" in value:
        raise ValueError(f"{name} must not contain NUL characters")


def _platform_for(agent_id: str) -> str:
    for prefix, platform in (
        ("hermes-", "hermes"),
        ("antigravity-", "antigravity"),
        ("claude-", "claude"),
    ):
        if agent_id.startswith(prefix):
            return platform
    return "spark"


class VortexStore:
    """Synchronous, thread-safe SQLite store; no message body is written to logs."""

    def __init__(
        self,
        path: str | Path,
        agents: dict[str, str] | None = None,
        approval_required_for: tuple[str, ...] | list[str] | None = None,
        *,
        schema: str = "forge",
    ):
        self.path = str(path)
        self._agents = dict(DEFAULT_AGENTS)
        if agents:
            self._agents.update(agents)
        self.set_approval_policy(approval_required_for)
        self._lock = threading.RLock()
        self._db: Database = open_database(self.path, schema=schema)
        self._initialize()

    def _initialize(self) -> None:
        self._db.prepare()
        with self._lock, self._db.transaction():
            for agent_id, display_name in self._agents.items():
                platform = _platform_for(agent_id)
                self._db.execute(
                    "INSERT INTO agents(agent_id,display_name,platform,created_at) VALUES(?,?,?,?)"
                    " ON CONFLICT DO NOTHING",
                    (agent_id, display_name, platform, iso(utc_now())),
                )

    def _task_access(self, agent_id: str, task_id: str) -> Mapping[str, Any]:
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

    def _task_view(self, row: Mapping[str, Any]) -> dict[str, Any]:
        return self._task_views([row])[0]

    def _task_views(self, rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        task_ids = [row["task_id"] for row in rows]
        placeholders = ", ".join("?" for _ in task_ids)
        dependencies_by_task = {task_id: [] for task_id in task_ids}
        blocked_by_task = {task_id: [] for task_id in task_ids}
        dependencies = self._db.execute(
            f"""
            SELECT d.task_id, d.depends_on, parent.status
            FROM task_dependencies d
            JOIN tasks parent ON parent.task_id=d.depends_on
            WHERE d.task_id IN ({placeholders})
            ORDER BY d.task_id, d.depends_on
            """,
            tuple(task_ids),
        ).fetchall()
        for dependency in dependencies:
            task_id = dependency["task_id"]
            depends_on = dependency["depends_on"]
            dependencies_by_task[task_id].append(depends_on)
            if dependency["status"] != "completed":
                blocked_by_task[task_id].append(depends_on)

        tasks = []
        for row in rows:
            task = dict(row)
            task_id = task["task_id"]
            task["depends_on"] = dependencies_by_task[task_id]
            task["blocked_by"] = blocked_by_task[task_id]
            task["awaiting_approval"] = task["approval_state"] == "pending"
            task["ready"] = (
                task["status"] == "open"
                and not task["blocked_by"]
                and task["approval_state"] in CLAIMABLE_APPROVAL_STATES
            )
            tasks.append(task)
        return tasks

    # --- Approval policy -----------------------------------------------------

    def set_approval_policy(self, approval_required_for: tuple[str, ...] | list[str] | None) -> None:
        """'*' gates every task; agent IDs gate tasks assigned to them; empty gates nothing."""
        entries = tuple(approval_required_for or ())
        self._approval_all = "*" in entries
        self._approval_assignees = frozenset(entry for entry in entries if entry != "*")

    def _approval_required(self, assigned_to: str | None, requested: bool) -> bool:
        if requested or self._approval_all:
            return True
        return assigned_to is not None and assigned_to in self._approval_assignees

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
        requires_approval: bool = False,
    ) -> dict[str, Any]:
        if not isinstance(requires_approval, bool):
            raise ValueError("requires_approval must be a boolean")  # noqa: TRY004 - ValueError is the API contract
        needs_approval = self._approval_required(assigned_to, requires_approval)
        if not isinstance(goal, str):
            raise ValueError("Task goal must be a string")  # noqa: TRY004 - ValueError is the API contract
        goal = goal.strip()
        if not goal:
            raise ValueError("Task goal must not be empty")
        if len(goal) > MAX_TASK_GOAL_CHARS:
            raise ValueError(f"Task goal exceeds {MAX_TASK_GOAL_CHARS} characters")
        _check_text("Task goal", goal)
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

        with self._lock, self._db.transaction():
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
                        or (existing["approval_state"] != "not_required") != needs_approval
                    ):
                        raise ValueError("Idempotency key was already used for a different task")
                    return existing_view

            task_id = str(uuid.uuid4())
            created_at = iso(utc_now())
            cursor = self._db.execute(
                """
                INSERT INTO tasks(
                    task_id,goal,created_by,assigned_to,priority,created_at,updated_at,idempotency_key,
                    approval_state
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT DO NOTHING
                """,
                (
                    task_id, goal, creator_id, assigned_to, priority, created_at, created_at,
                    idempotency_key, "pending" if needs_approval else "not_required",
                ),
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
                    or (existing["approval_state"] != "not_required") != needs_approval
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
                {
                    "assigned_to": assigned_to,
                    "depends_on": sorted(dependencies),
                    "priority": priority,
                    "requires_approval": needs_approval,
                },
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
        requires_approval: bool = False,
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

        with self._lock, self._db.transaction():
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
                        requires_approval=requires_approval,
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
            return self._task_views(rows)

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
            raise ValueError("Lease duration must be an integer")  # noqa: TRY004 - ValueError is the API contract
        if not 30 <= lease_seconds <= MAX_TASK_LEASE_SECONDS:
            raise ValueError(f"Lease duration must be between 30 and {MAX_TASK_LEASE_SECONDS} seconds")
        with self._lock, self._db.transaction():
            if self._get_agent(self._db, agent_id) is None:
                raise ValueError("Unknown agent identity")
            self._require_not_paused(agent_id)
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
            if row["approval_state"] == "pending":
                raise PermissionError("Task is awaiting operator approval and cannot be claimed yet")
            if row["approval_state"] not in CLAIMABLE_APPROVAL_STATES:
                raise PermissionError(f"Task approval state is {row['approval_state']}; it cannot be claimed")
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
                  AND approval_state IN ('not_required','approved')
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
        if isinstance(blocked_reason, str):
            _check_text("Blocked reason", blocked_reason)
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

        with self._lock, self._db.transaction():
            row = self._task_access(agent_id, task_id)
            self._require_not_paused(agent_id)
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
        _check_text("Task result", result)
        with self._lock, self._db.transaction():
            row = self._task_access(agent_id, task_id)
            self._require_not_paused(agent_id)
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
        if isinstance(reason, str):
            _check_text("Reason", reason)
        if not isinstance(reason, str) or len(reason) > 1000:
            raise ValueError("Cancellation reason must be a string of at most 1000 characters")
        with self._lock, self._db.transaction():
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
        with self._lock, self._db.transaction():
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

    def _get_agent(self, conn: Database, agent_id: str) -> Mapping[str, Any] | None:
        return conn.execute("SELECT agent_id FROM agents WHERE agent_id=?", (agent_id,)).fetchone()

    def send(self, sender_id: str, recipient_id: str, body: str,
             thread_id: str | None = None, reply_to: str | None = None,
             task_id: str | None = None) -> dict[str, Any]:
        body = body.strip()
        if not body:
            raise ValueError("Message body must not be empty")
        if len(body) > MAX_MESSAGE_CHARS:
            raise ValueError(f"Message body exceeds {MAX_MESSAGE_CHARS} characters")
        _check_text("Message body", body)
        if sender_id == recipient_id:
            raise ValueError("Cannot send a message to the same agent identity")
        with self._lock, self._db.transaction():
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
                query += " AND (created_at, message_id) > (?, ?)"
                args.extend((cursor["created_at"], cursor["message_id"]))
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

        placeholders = ", ".join("?" for _ in message_ids)
        with self._lock, self._db.transaction():
            rows = self._db.execute(
                f"SELECT * FROM messages WHERE message_id IN ({placeholders})",
                tuple(message_ids),
            ).fetchall()
            if len(rows) != len(message_ids):
                raise ValueError("Message not found")
            for row in rows:
                if row["recipient_id"] != agent_id:
                    raise PermissionError("Only the addressed recipient can acknowledge a message")

            acked_at = iso(utc_now())
            self._db.execute(
                f"UPDATE messages SET acked_at = ? WHERE message_id IN ({placeholders}) AND acked_at IS NULL",
                (acked_at, *message_ids),
            )

            rows = self._db.execute(
                f"SELECT * FROM messages WHERE message_id IN ({placeholders})",
                tuple(message_ids),
            ).fetchall()
            by_message_id = {row["message_id"]: dict(row) for row in rows}
            return [by_message_id[message_id] for message_id in message_ids]

    def acknowledge(self, agent_id: str, message_id: str) -> dict[str, Any]:
        with self._lock, self._db.transaction():
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

    def message_context(self, agent_id: str, message_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._db.execute(
                "SELECT thread_id,sender_id,recipient_id FROM messages WHERE message_id=?",
                (message_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Message not found")
        if agent_id not in (row["sender_id"], row["recipient_id"]):
            raise PermissionError("Only participants can read this thread")
        return dict(row)

    def thread(self, agent_id: str, message_id: str) -> list[dict[str, Any]]:
        message = self.message_context(agent_id, message_id)
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE thread_id=? ORDER BY created_at", (message["thread_id"],)).fetchall()
        return [dict(row) for row in rows]

    # --- Operator oversight -------------------------------------------------
    # These methods bypass creator/assignee scoping. The server layer is the
    # authority on who is an operator; it must check before calling them.

    def operator_overview(self, operator_id: str, stale_after_seconds: int = 90) -> dict[str, Any]:
        """Fleet-wide snapshot: agent presence plus task counts by status and agent."""
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            now_text = iso(utc_now())
            task_rows = self._db.execute(
                """
                SELECT assigned_to, status, COUNT(*) AS n,
                       SUM(CASE
                           WHEN status='in_progress' AND lease_expires_at IS NOT NULL
                                AND lease_expires_at <= ? THEN 1 ELSE 0
                       END) AS expired,
                       SUM(CASE
                           WHEN approval_state='pending' AND status='open' THEN 1 ELSE 0
                       END) AS pending_approvals
                FROM tasks
                GROUP BY assigned_to, status
                """,
                (now_text,),
            ).fetchall()
            unread = self._db.execute(
                "SELECT recipient_id, COUNT(*) AS n FROM messages WHERE acked_at IS NULL GROUP BY recipient_id"
            ).fetchall()
        counts = {status: 0 for status in TASK_STATUSES}
        expired = 0
        pending_approvals = 0
        active_by_agent: dict[str, dict[str, int]] = {}
        for row in task_rows:
            counts[row["status"]] += row["n"]
            expired += row["expired"]
            pending_approvals += row["pending_approvals"]
            if row["assigned_to"] is not None and row["status"] in {"open", "in_progress", "blocked"}:
                active_by_agent.setdefault(row["assigned_to"], {})[row["status"]] = row["n"]
        unread_by_agent = {row["recipient_id"]: row["n"] for row in unread}
        agents = self.list_agents(operator_id, stale_after_seconds=stale_after_seconds)
        for agent in agents:
            agent["active_tasks"] = active_by_agent.get(agent["agent_id"], {})
            agent["unread_messages"] = unread_by_agent.get(agent["agent_id"], 0)
        return {
            "generated_at": now_text,
            "agents": agents,
            "task_counts": counts,
            "expired_leases": expired,
            "pending_approvals": pending_approvals,
            "paused_agents": [agent["agent_id"] for agent in agents if agent.get("paused_at")],
        }

    def operator_list_tasks(
        self,
        operator_id: str,
        status: str | None = None,
        assigned_to: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List any task in the fleet, optionally filtered by status and assignee."""
        if status is not None and status not in TASK_STATUSES:
            raise ValueError("Unknown task status")
        limit = max(1, min(int(limit), 200))
        query = "SELECT * FROM tasks WHERE 1=1"
        args: list[Any] = []
        if status is not None:
            query += " AND status=?"
            args.append(status)
        if assigned_to is not None:
            query += " AND assigned_to=?"
            args.append(assigned_to)
        query += " ORDER BY priority,updated_at DESC,task_id LIMIT ?"
        args.append(limit)
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            if assigned_to is not None and self._get_agent(self._db, assigned_to) is None:
                raise ValueError("Unknown assignee identity")
            rows = self._db.execute(query, args).fetchall()
            return self._task_views(rows)

    def operator_get_task(self, operator_id: str, task_id: str) -> dict[str, Any]:
        """Any task plus its full lifecycle history."""
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            task = self._task_view(row)
            events = [
                dict(event)
                for event in self._db.execute(
                    "SELECT * FROM task_events WHERE task_id=? ORDER BY event_id", (task_id,)
                ).fetchall()
            ]
        for event in events:
            event["details"] = json.loads(event.pop("details_json"))
        task["events"] = events
        return task

    def operator_cancel_task(self, operator_id: str, task_id: str, reason: str) -> dict[str, Any]:
        """Cancel any non-terminal task; the operator and reason are recorded in the audit trail."""
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Operator cancellation requires a non-empty reason")
        _check_text("Reason", reason)
        if len(reason) > 1000:
            raise ValueError("Cancellation reason must be at most 1000 characters")
        with self._lock, self._db.transaction():
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            if row["status"] in {"completed", "cancelled"}:
                raise ValueError(f"Task is already {row['status']}")
            now_text = iso(utc_now())
            cursor = self._db.execute(
                """
                UPDATE tasks SET status='cancelled',lease_expires_at=NULL,updated_at=?
                WHERE task_id=? AND status NOT IN ('completed','cancelled')
                """,
                (now_text, task_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task became terminal before cancellation")
            self._task_event(
                task_id,
                operator_id,
                "cancelled",
                {"reason": reason.strip(), "by_operator": True, "previous_status": row["status"]},
                now_text,
            )
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def operator_events(
        self,
        operator_id: str,
        after_event_id: int = 0,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Fleet-wide task event feed in order; pass the last event_id to continue."""
        if isinstance(after_event_id, bool) or not isinstance(after_event_id, int) or after_event_id < 0:
            raise ValueError("after_event_id must be a non-negative integer")
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            rows = self._db.execute(
                """
                SELECT e.*, t.goal AS task_goal, t.status AS task_status, t.assigned_to AS task_assigned_to
                FROM task_events e JOIN tasks t ON t.task_id=e.task_id
                WHERE e.event_id > ? ORDER BY e.event_id LIMIT ?
                """,
                (after_event_id, limit),
            ).fetchall()
            events = [dict(row) for row in rows]
        for event in events:
            event["details"] = json.loads(event.pop("details_json"))
        return events

    def operator_list_pending_approvals(self, operator_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Open tasks waiting on an operator decision, highest priority and oldest first."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            rows = self._db.execute(
                """
                SELECT * FROM tasks WHERE approval_state='pending' AND status='open'
                ORDER BY priority,created_at,task_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return self._task_views(rows)

    def _decide_approval(
        self, operator_id: str, task_id: str, approve: bool, note: str
    ) -> dict[str, Any]:
        if not isinstance(note, str) or len(note) > MAX_APPROVAL_NOTE_CHARS:
            raise ValueError(f"Approval note must be a string of at most {MAX_APPROVAL_NOTE_CHARS} characters")
        _check_text("Approval note", note)
        note = note.strip()
        if not approve and not note:
            raise ValueError("Rejecting a task requires a non-empty reason")
        with self._lock, self._db.transaction():
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            row = self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("Task not found")
            if row["approval_state"] != "pending":
                raise ValueError(f"Task is not awaiting approval (approval state: {row['approval_state']})")
            if row["status"] != "open":
                raise ValueError(f"Task cannot be decided while status is {row['status']}")
            now_text = iso(utc_now())
            if approve:
                cursor = self._db.execute(
                    """
                    UPDATE tasks SET approval_state='approved',updated_at=?
                    WHERE task_id=? AND approval_state='pending' AND status='open'
                    """,
                    (now_text, task_id),
                )
            else:
                cursor = self._db.execute(
                    """
                    UPDATE tasks SET approval_state='rejected',status='cancelled',updated_at=?
                    WHERE task_id=? AND approval_state='pending' AND status='open'
                    """,
                    (now_text, task_id),
                )
            if cursor.rowcount != 1:
                raise ValueError("Task approval was decided concurrently")
            self._task_event(
                task_id,
                operator_id,
                "approved" if approve else "rejected",
                {"note": note} if approve else {"reason": note},
                now_text,
            )
            return self._task_view(
                self._db.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            )

    def operator_approve_task(self, operator_id: str, task_id: str, note: str = "") -> dict[str, Any]:
        """Release a pending task so its assignee (or any agent, if unassigned) can claim it."""
        return self._decide_approval(operator_id, task_id, approve=True, note=note)

    def operator_reject_task(self, operator_id: str, task_id: str, reason: str) -> dict[str, Any]:
        """Refuse a pending task: it is cancelled and can never be claimed; reason required."""
        return self._decide_approval(operator_id, task_id, approve=False, note=reason)

    # --- Kill switch ---------------------------------------------------------

    def _require_not_paused(self, agent_id: str) -> None:
        row = self._db.execute(
            "SELECT paused_at, paused_reason FROM agents WHERE agent_id=?", (agent_id,)
        ).fetchone()
        if row is not None and row["paused_at"]:
            reason = f": {row['paused_reason']}" if row["paused_reason"] else ""
            raise PermissionError(f"Agent {agent_id} is paused by an operator{reason}")

    def _operator_action(
        self, actor_id: str, action: str, target_id: str | None, details: dict[str, Any], created_at: str
    ) -> None:
        self._db.execute(
            "INSERT INTO operator_actions(actor_id,action,target_id,details_json,created_at) VALUES(?,?,?,?,?)",
            (actor_id, action, target_id, json.dumps(details, sort_keys=True), created_at),
        )

    def operator_pause_agent(
        self, operator_id: str, agent_id: str, reason: str, cancel_active: bool = False
    ) -> dict[str, Any]:
        """Stop an agent: it can no longer claim, renew or complete work until resumed.

        With cancel_active, its in-progress and blocked tasks are cancelled (audited);
        otherwise their leases simply lapse and other agents may reclaim them.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Pausing an agent requires a non-empty reason")
        _check_text("Reason", reason)
        if len(reason) > MAX_APPROVAL_NOTE_CHARS:
            raise ValueError(f"Reason must be at most {MAX_APPROVAL_NOTE_CHARS} characters")
        if not isinstance(cancel_active, bool):
            raise ValueError("cancel_active must be a boolean")  # noqa: TRY004 - ValueError is the API contract
        if agent_id == operator_id:
            raise ValueError("An operator cannot pause its own identity")
        reason = reason.strip()
        with self._lock, self._db.transaction():
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            if self._get_agent(self._db, agent_id) is None:
                raise ValueError("Unknown agent identity")
            now_text = iso(utc_now())
            self._db.execute(
                "UPDATE agents SET paused_at=?, paused_reason=? WHERE agent_id=?",
                (now_text, reason, agent_id),
            )
            cancelled: list[str] = []
            if cancel_active:
                rows = self._db.execute(
                    "SELECT task_id, status FROM tasks WHERE assigned_to=? AND status IN ('in_progress','blocked')",
                    (agent_id,),
                ).fetchall()
                for row in rows:
                    self._db.execute(
                        "UPDATE tasks SET status='cancelled',lease_expires_at=NULL,updated_at=? WHERE task_id=?",
                        (now_text, row["task_id"]),
                    )
                    self._task_event(
                        row["task_id"], operator_id, "cancelled",
                        {"reason": f"agent paused: {reason}", "by_operator": True, "previous_status": row["status"]},
                        now_text,
                    )
                    cancelled.append(row["task_id"])
            self._operator_action(
                operator_id, "pause_agent", agent_id,
                {"reason": reason, "cancel_active": cancel_active, "cancelled_tasks": cancelled}, now_text,
            )
            agent = dict(self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone())
        return {"agent": agent, "cancelled_tasks": cancelled}

    def operator_resume_agent(self, operator_id: str, agent_id: str) -> dict[str, Any]:
        """Lift a pause so the agent can claim work again."""
        with self._lock, self._db.transaction():
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            row = self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
            if row is None:
                raise ValueError("Unknown agent identity")
            if not row["paused_at"]:
                raise ValueError(f"Agent {agent_id} is not paused")
            now_text = iso(utc_now())
            self._db.execute(
                "UPDATE agents SET paused_at=NULL, paused_reason=NULL WHERE agent_id=?", (agent_id,)
            )
            self._operator_action(
                operator_id, "resume_agent", agent_id, {"was_paused_at": row["paused_at"]}, now_text
            )
            return dict(self._db.execute("SELECT * FROM agents WHERE agent_id=?", (agent_id,)).fetchone())

    def operator_audit_log(self, operator_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent operator actions (pause/resume), newest first."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            if self._get_agent(self._db, operator_id) is None:
                raise ValueError("Unknown operator identity")
            rows = self._db.execute(
                "SELECT * FROM operator_actions ORDER BY action_id DESC LIMIT ?", (limit,)
            ).fetchall()
            entries = [dict(row) for row in rows]
        for entry in entries:
            entry["details"] = json.loads(entry.pop("details_json"))
        return entries

    def acquire_instance_lock(self) -> None:
        """On Postgres, refuse to run if another Vortex server already uses this schema."""
        if self._db.dialect == "postgres":
            self._db.acquire_instance_lock()

    def close(self) -> None:
        with self._lock:
            self._db.close()
