# Vortex

Vortex is a secure coordination MCP for a small, explicit set of agent instances. Its first target deployment is two Hermes instances, one Antigravity instance, and one Gemini Spark instance.

## Current status

Two auth modes ship, selected by `VORTEX_AUTH_MODE`:

| Mode | What authenticates | Status |
|---|---|---|
| `dev` (default) | Four static loopback bearer tokens | Working |
| `oauth` | OAuth 2.1 bearer tokens introspected against an identity provider | **Verified end-to-end against Keycloak 26.8.0** |

In `oauth` mode the server introspects each incoming bearer token, maps the token's client to a Vortex identity, and requires the token's audience to contain `VORTEX_PUBLIC_URL + "/mcp"`. Identity is derived from the token, never from tool arguments — a token for `hermes-1` cannot act as `hermes-2`. Dev mode only accepts loopback binding; OAuth mode requires HTTPS at the public URL.

Verified locally: Keycloak realm `vortex` issues `client_credentials` tokens for four agent clients plus a `vortex-introspection` client; a two-identity run minted separate tokens, registered both agents, sent a message from `hermes-1` to `hermes-2`, replied and acked round-trip, rejected a bogus token with `401`, and rejected a request with no token with `401`.

MCP tools alone do not wake an inactive agent. Vortex messages are queued and delivered when the recipient polls; no platform connector or push/wake-up integration is implemented.

## Operator role

Identities listed in `VORTEX_OPERATOR_IDS` get fleet-wide oversight; everyone else keeps creator/assignee scoping. The list is empty by default, and every operator must also be a configured agent ID (startup fails closed otherwise). Operator status comes from the authenticated token, never a tool argument.

| Tool | Purpose |
|---|---|
| `vortex_operator_overview` | Agent presence, active tasks per agent, unread counts, task totals, expired leases |
| `vortex_operator_list_tasks` | Any task, filtered by `status` / `assigned_to` |
| `vortex_operator_get_task` | Any task plus its full event history |
| `vortex_operator_cancel_task` | Cancel any non-terminal task; reason required, recorded with `by_operator: true` |
| `vortex_operator_events` | Fleet-wide event feed; page with `after_event_id` |

Example: add `claude-operator` to `VORTEX_AGENT_IDS`, set `VORTEX_TOKEN_CLAUDE_OPERATOR`, and set `VORTEX_OPERATOR_IDS=claude-operator`.

### Approval gate

A task that requires approval is created with `approval_state: "pending"` and cannot be claimed by anyone until an operator decides. Approval makes it claimable; rejection cancels it permanently. Every decision is an audited task event.

A task requires approval when either:

- its creator passes `requires_approval=true` to `vortex_create_task` or `vortex_dispatch_tasks`, or
- server policy `VORTEX_APPROVAL_REQUIRED_FOR` matches: `*` gates every task, or a list of agent IDs gates tasks assigned to them. Creators cannot opt out of policy. A list does not gate unassigned tasks; use `*` for that.

Policy requires at least one operator (startup fails closed otherwise). Existing databases migrate in place; tasks created before the upgrade need no approval.

| Tool | Purpose |
|---|---|
| `vortex_operator_list_pending_approvals` | Open tasks awaiting a decision, by priority then age |
| `vortex_operator_approve_task` | Make a pending task claimable; optional note |
| `vortex_operator_reject_task` | Cancel a pending task permanently; reason required |

`vortex_operator_overview` also reports `pending_approvals`.

### Kill switch

| Tool | Purpose |
|---|---|
| `vortex_operator_pause_agent` | Stop an agent claiming, renewing or completing work; reason required. `cancel_active=true` also cancels its in-progress and blocked tasks |
| `vortex_operator_resume_agent` | Lift the pause |
| `vortex_operator_audit_log` | Recent operator actions (pause/resume), newest first |

`vortex_operator_overview` lists `paused_agents`. Existing databases migrate in place.

### Live event stream

`GET /events` streams task events as Server-Sent Events for an **operator** bearer token (401 without a valid token, 403 for a non-operator). Resume with `Last-Event-ID` or `?after=<event_id>`; `?once=1` returns what is available and closes, for simple polling. Browsers cannot set the `Authorization` header on `EventSource`, so put a backend that holds the token in front of a web console.

```sh
curl -N -H "Authorization: Bearer $VORTEX_OPERATOR_TOKEN" http://127.0.0.1:8000/events
```

## Adapter SDK

`vortex.sdk.VortexAgent` turns any Python system into a fleet agent. It heartbeats, claims ready tasks addressed to it, runs the matching handler and completes the task with the result. Handler errors and unknown actions mark the task **blocked** with the reason, so the operator sees them. Pauses and approval gates are enforced by the server; a paused agent just skips work.

```python
from vortex.sdk import VortexAgent, make_goal

agent = VortexAgent("http://127.0.0.1:8000/mcp", token=os.environ["VORTEX_TOKEN"])

@agent.action("restart_gateway")
def restart(args):
    return {"restarted": True}

agent.run_forever()
```

Dispatch work to it with `vortex_create_task(goal=make_goal("restart_gateway", {...}), assigned_to="<agent>")`.

### Ready-made adapters

Each adapter is its own Vortex identity: add it to `VORTEX_AGENT_IDS` with a token. Gate anything that changes state with `VORTEX_APPROVAL_REQUIRED_FOR=<that id>`.

| Adapter | Run | Actions |
|---|---|---|
| HORIZONTAL Sovereign gateway | `python -m vortex.adapters.horizontal` (`HORIZONTAL_GATEWAY_URL`, `CITADEL_AUTH_TOKEN`) | `status`, `health`, `cluster_status`, `techtree_status`, `diamond_status`, `circadian_status`, `search {q}`, `command {command, ...}`. Commands are limited to read-only/dry-run `scan`, `diagnose`, `improve`, `self_refactor`, `vector_search` unless opted in via `HORIZONTAL_ADAPTER_EXTRA_COMMANDS` |
| Sentinel | `python -m vortex.adapters.sentinel` (`SENTINEL_URL`) | `health`, `diagnostics`, `quota_status`. With a dedicated Firebase user (`SENTINEL_FIREBASE_API_KEY`, `SENTINEL_REFRESH_TOKEN`): `health_history`, `debate_telemetry`, and opt-in `execute_healing {action_type}` (`SENTINEL_ADAPTER_ALLOW_HEALING=1`) |

The adapters reach their systems on loopback, so those systems never need to be exposed; Vortex is the only entry point.

## Watcher

`python -m vortex.watcher` is a small non-AI loop using an operator token. It alerts only when something changes:
- a task starts waiting for approval
- expired leases appear
- an agent goes offline
- a task becomes blocked
- an agent is paused

Delivery goes to the log, plus optionally phone push via [ntfy](https://ntfy.sh) (`VORTEX_ALERT_NTFY_TOPIC`), a Slack/Discord-style webhook (`VORTEX_ALERT_WEBHOOK`), or a command run without a shell (`VORTEX_ALERT_COMMAND`, for example to start a Claude session). See `.env.example`.

## Storage

Vortex stores agents, messages, tasks and audit records in SQLite by default (`<VORTEX_DATA_DIR>/vortex.sqlite3`).
Set `VORTEX_STORE_URL` to run it on Postgres (Supabase) instead; agent-facing tools behave the same on both.

| Variable | Meaning |
|---|---|
| `VORTEX_STORE_URL` | Unset: SQLite. A `postgresql://` connection string: Postgres, connecting as `vortex_writer`. Never logged. |
| `VORTEX_SCHEMA` | Postgres schema to use (default `forge`). |
| `VORTEX_MIGRATE_URL` | Admin connection used only by `python -m vortex migrate`. |

1. **Migrate once, as an admin:** `VORTEX_MIGRATE_URL=<admin connection> python -m vortex migrate`. It creates the
   schema and tables and a `vortex_writer` role (`NOLOGIN`) with row access only, no DDL. Give a login role
   membership of `vortex_writer` (or `LOGIN` with its own password) and use that in `VORTEX_STORE_URL`.
2. **Use a direct or session-mode connection,** not Supabase's transaction-mode pooler: the schema search path
   and the single-instance lock are session state.
3. **One server per schema.** Vortex holds a Postgres advisory lock; a second server on the same schema refuses to
   start, and a server that loses the lock after a reconnect refuses every call until restarted.
4. **Ledger relay.** Every task event and operator action is also written to an `outbox` table in the same
   transaction. `python -m vortex relay` (install with the `unisync` extra; needs `VORTEX_STORE_URL`,
   `UNISYNC_GATEWAY_URL`, `UNISYNC_VORTEX_TOKEN`) copies each row to the UniSync ledger exactly once, as a
   `memory.write` from `vortex` in scope `forge`, key `outbox/<id>`. It backs off up to 5 minutes on failure and
   never logs payloads or tokens.

Tests run on SQLite by default; set `VORTEX_TEST_DATABASE_URL` to a throwaway Postgres to run the same suite there
(CI does both).

## Development

To add more configured agent connections, set `VORTEX_AGENT_IDS` to a comma-separated list such as `hermes-1,hermes-2,antigravity-1,spark-1,oracle-1`. The server then requires matching `VORTEX_TOKEN_<AGENT>` and OAuth client mapping values for every configured identity. Preferred config names are `VORTEX_CLIENT_<AGENT>`; legacy `VORTEX_SUBJECT_<AGENT>` values still work for compatibility.

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --python 3.11
uv run --python 3.11 python -m unittest discover -s tests -v
```

Copy `.env.example` to `.env`, then set four unique high-entropy values (at least 32 characters) for `VORTEX_TOKEN_HERMES_1`, `VORTEX_TOKEN_HERMES_2`, `VORTEX_TOKEN_ANTIGRAVITY_1`, and `VORTEX_TOKEN_SPARK_1`. Keep `.env` private; it is excluded from Git. Start the development server with `uv run --python 3.11 python -m vortex`. Development mode only accepts loopback binding.

### OAuth mode

Set `VORTEX_AUTH_MODE=oauth` plus the `VORTEX_OAUTH_*` block in `.env.example`. Every field is required in this mode; startup fails closed on any missing or invalid value.

- `VORTEX_PUBLIC_URL` must be HTTPS without credentials. It sets both the OAuth audience and the MCP `resource_url`, so it must match exactly how clients reach the server.
- `VORTEX_ALLOWED_HOSTS` / `VORTEX_ALLOWED_ORIGINS` are matched against the `Host` and `Origin` headers. The MCP transport matches the `Host` value **exactly including the port**, or via a `:*` wildcard (`localhost:*`). A bare `localhost` will not match `Host: localhost:8000`.
- `VORTEX_CLIENT_<AGENT>` maps each Keycloak client to its Vortex identity, e.g. `VORTEX_CLIENT_HERMES_1=hermes-1`. The legacy `VORTEX_SUBJECT_<AGENT>` name is still accepted for compatibility.
- `VORTEX_OAUTH_CA_BUNDLE` optionally names a PEM bundle that augments the system trust store. Needed when the identity provider uses an internal or Tailscale-signed certificate that `httpx` does not trust by default; startup fails if the path does not exist.

## First-slice tools

- `vortex_register_agent`: validate/register one of the four configured instance identities and refresh its presence.
- `vortex_list_agents`: list connection/presence metadata (no credentials).
- `vortex_send_message`: send a bounded message to one instance; optionally include `task_id` to attach it to a task visible to both sender and recipient.
- `vortex_get_inbox`: read pending messages addressed to the authenticated instance, with stable cursor pagination (`after_message_id`) and optional acknowledged-message history.
- `vortex_ack_message`: acknowledge delivery/read of an inbox message.
- `vortex_ack_messages`: atomically acknowledge 1-100 inbox messages; the whole batch is rejected if any message is missing or belongs to another instance.
- `vortex_get_thread`: read the complete thread for a message involving the authenticated instance.
- `vortex_reply_message`: send a threaded reply to a message's sender.

Inbox pages are ordered by creation time and message ID. To fetch the next page, pass the last message's `message_id` as `after_message_id`; cursors are scoped to the authenticated recipient.

## Durable work coordination

Work items are separate from messages: they have a goal, creator, optional assignee, priority (1 is highest), dependencies, status, lease, result, and lifecycle event history. SQLite persists tasks and events across server restarts.

- `vortex_create_task`: create work, optionally assign it, attach up to 20 dependencies, and provide an idempotency key for safe retries. Reusing a key with different task details is rejected.
- `vortex_dispatch_tasks`: create one task per recipient in one atomic fan-out call; recipients must be configured agents, and each task is assigned to its own recipient.
- `vortex_list_tasks` / `vortex_get_task`: discover or inspect work. Unassigned open tasks are visible to configured agents; assigned tasks are visible to their creator and assignee.
- `vortex_claim_task`: claim open work, resume your blocked task, renew your lease, or reclaim an expired in-progress lease. A task with dependencies cannot be claimed until all dependencies are complete. After a reclaim, the former assignee loses update/complete access.
- `vortex_update_task`: report progress or a blocked reason. Blocking releases the lease; the assigned agent can resume by claiming the task again.
- `vortex_complete_task` / `vortex_cancel_task`: record a result or stop non-terminal work. Completion requires an active lease; cancellation is allowed to the creator or assignee.
- `vortex_get_task_events`: inspect task lifecycle history.

Leases are time-bounded (30–3600 seconds, default 300). They provide recoverable ownership, not exactly-once execution: external side effects must still be idempotent. Task event records are persisted for auditability, and task-linked messages appear in inbox/thread records with their `task_id`. This initial slice does not yet implement event-stream push, retention/cleanup, cross-project access scopes, or official MCP Tasks extension negotiation. Until those are implemented, clients should poll task state and use the existing inbox for messages.

## SPINE organ health survey

SPINE adds two authenticated, local-only tools:

- `vortex_spine_survey`: validate and classify entries in an operator-configured JSON registry; returns organ ID, role, status, canon flag, and a SHA-256 registry fingerprint, never filesystem paths.
- `vortex_spine_plan`: preview bounded read-only advisory tasks. It creates local SQLite task records only when `create_tasks=true`; each record is attributed to the authenticated creator and assigned to one of the configured agent identities. Idempotency keys prevent duplicate records for the same registry snapshot. Creating records does not wake or remotely dispatch agents.

Before enabling these tools, configure `VORTEX_SPINE_REGISTRY` to an absolute path for a JSON file with an `organs` list, and `VORTEX_SPINE_ORGAN_ROOTS` to one or more absolute, existing roots separated by the platform path separator. Each organ entry accepts only `id`, `role`, `expected` (`present` or `absent`), `path`, optional `canon` (`yes`, `no`, or `undecided`), and optional relative `sentinel`. Organ paths and sentinel checks are limited to those roots; symlinks/junctions, traversal, secret-named path components, oversized registries, extra schema fields, and invalid entries are rejected. Survey results and task goals omit local paths. With either setting missing, SPINE tools fail closed. No paths or file contents are written into task records.

The implementation intentionally does not execute shell commands, read/write arbitrary files, or claim to dispatch work into idle agents. Individual MCP clients authenticate as distinct Vortex identities.
