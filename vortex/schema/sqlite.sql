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
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox(sent_at, outbox_id);
