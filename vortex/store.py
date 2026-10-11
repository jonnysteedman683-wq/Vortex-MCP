"""SQLite persistence for Vortex agent presence and messages."""
from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_AGENTS = {
    "hermes-1": "Hermes instance 1",
    "hermes-2": "Hermes instance 2",
    "antigravity-1": "Antigravity instance 1",
    "spark-1": "Gemini Spark instance 1",
}
MAX_MESSAGE_CHARS = 8000


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
                    reply_to TEXT REFERENCES messages(message_id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_inbox
                    ON messages(recipient_id, acked_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_thread
                    ON messages(thread_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_messages_reply_to
                    ON messages(reply_to);
            """)
            for agent_id, display_name in self._agents.items():
                platform = "hermes" if agent_id.startswith("hermes-") else (
                    "antigravity" if agent_id.startswith("antigravity-") else "spark"
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO agents(agent_id,display_name,platform,created_at) VALUES(?,?,?,?)",
                    (agent_id, display_name, platform, iso(utc_now())),
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
             thread_id: str | None = None, reply_to: str | None = None) -> dict[str, Any]:
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
            if thread_id and not reply_to:
                raise ValueError("Use reply_to to add a message to an existing thread")
            resolved_thread_id = thread_id
            if reply_to:
                parent = self._db.execute(
                    "SELECT thread_id,sender_id,recipient_id FROM messages WHERE message_id=?",
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
            message_id = str(uuid.uuid4())
            resolved_thread_id = resolved_thread_id or str(uuid.uuid4())
            created_at = iso(utc_now())
            self._db.execute(
                "INSERT INTO messages(message_id,thread_id,sender_id,recipient_id,body,created_at,reply_to) VALUES(?,?,?,?,?,?,?)",
                (message_id, resolved_thread_id, sender_id, recipient_id, body, created_at, reply_to),
            )
        return {"message_id": message_id, "thread_id": resolved_thread_id, "sender_id": sender_id,
                "recipient_id": recipient_id, "body": body, "created_at": created_at,
                "reply_to": reply_to, "delivery": "queued"}

    def inbox(self, agent_id: str, limit: int = 20, include_acked: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        query = "SELECT * FROM messages WHERE recipient_id=?"
        args: list[Any] = [agent_id]
        if not include_acked:
            query += " AND acked_at IS NULL"
        query += " ORDER BY created_at LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(query, args).fetchall()
        return [dict(row) for row in rows]

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
