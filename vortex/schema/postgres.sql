-- Vortex store schema for Postgres, created in the current search_path schema by `python -m vortex migrate`.
-- Every text column uses COLLATE "C" so ISO-8601 timestamps and ids sort byte-wise, exactly as in SQLite.
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT COLLATE "C" PRIMARY KEY,
    display_name TEXT COLLATE "C" NOT NULL,
    platform TEXT COLLATE "C" NOT NULL,
    status TEXT COLLATE "C" NOT NULL DEFAULT 'offline',
    last_seen TEXT COLLATE "C",
    created_at TEXT COLLATE "C" NOT NULL,
    paused_at TEXT COLLATE "C",
    paused_reason TEXT COLLATE "C"
);
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT COLLATE "C" PRIMARY KEY,
    goal TEXT COLLATE "C" NOT NULL,
    created_by TEXT COLLATE "C" NOT NULL REFERENCES agents(agent_id),
    assigned_to TEXT COLLATE "C" REFERENCES agents(agent_id),
    status TEXT COLLATE "C" NOT NULL DEFAULT 'open'
        CHECK(status IN ('open','in_progress','blocked','completed','cancelled')),
    priority INTEGER NOT NULL DEFAULT 3 CHECK(priority BETWEEN 1 AND 5),
    created_at TEXT COLLATE "C" NOT NULL,
    updated_at TEXT COLLATE "C" NOT NULL,
    lease_expires_at TEXT COLLATE "C",
    blocked_reason TEXT COLLATE "C",
    result TEXT COLLATE "C",
    idempotency_key TEXT COLLATE "C",
    approval_state TEXT COLLATE "C" NOT NULL DEFAULT 'not_required'
        CHECK(approval_state IN ('not_required','pending','approved','rejected')),
    UNIQUE(created_by, idempotency_key)
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT COLLATE "C" PRIMARY KEY,
    thread_id TEXT COLLATE "C" NOT NULL,
    sender_id TEXT COLLATE "C" NOT NULL REFERENCES agents(agent_id),
    recipient_id TEXT COLLATE "C" NOT NULL REFERENCES agents(agent_id),
    body TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C" NOT NULL,
    acked_at TEXT COLLATE "C",
    reply_to TEXT COLLATE "C" REFERENCES messages(message_id),
    task_id TEXT COLLATE "C" REFERENCES tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_inbox_cursor ON messages(recipient_id, acked_at, created_at, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_reply_to ON messages(reply_to);
CREATE INDEX IF NOT EXISTS idx_messages_task ON messages(task_id);
CREATE TABLE IF NOT EXISTS task_dependencies (
    task_id TEXT COLLATE "C" NOT NULL REFERENCES tasks(task_id),
    depends_on TEXT COLLATE "C" NOT NULL REFERENCES tasks(task_id),
    PRIMARY KEY(task_id, depends_on),
    CHECK(task_id != depends_on)
);
CREATE TABLE IF NOT EXISTS task_events (
    event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id TEXT COLLATE "C" NOT NULL REFERENCES tasks(task_id),
    actor_id TEXT COLLATE "C" NOT NULL REFERENCES agents(agent_id),
    event_type TEXT COLLATE "C" NOT NULL,
    details_json TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C" NOT NULL
);
CREATE TABLE IF NOT EXISTS operator_actions (
    action_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_id TEXT COLLATE "C" NOT NULL REFERENCES agents(agent_id),
    action TEXT COLLATE "C" NOT NULL,
    target_id TEXT COLLATE "C",
    details_json TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C" NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_creator_status ON tasks(created_by, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assigned_to, status, updated_at);
CREATE INDEX IF NOT EXISTS idx_tasks_pending_approval ON tasks(status, approval_state, priority, created_at, task_id);
CREATE INDEX IF NOT EXISTS idx_task_dependencies_parent ON task_dependencies(depends_on);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, event_id);
-- Ledger outbox: written in the same transaction as each task event and operator action.
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_kind TEXT COLLATE "C" NOT NULL,
    payload_json TEXT COLLATE "C" NOT NULL,
    created_at TEXT COLLATE "C" NOT NULL,
    sent_at TEXT COLLATE "C"
);
CREATE INDEX IF NOT EXISTS idx_outbox_unsent ON outbox(sent_at, outbox_id);
