"""Vortex Streamable HTTP MCP service for a fixed set of agent instances."""
from __future__ import annotations

import hashlib
import os
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .auth import IntrospectionTokenVerifier
from .settings import AGENT_IDS, Settings
from .spine import advisory_task_goals, survey_organs
from .store import VortexStore

load_dotenv()


def _loopback_url(host: str, port: int, path: str = "") -> str:
    host = "[::1]" if host == "::1" else host
    return f"http://{host}:{port}{path}"


class DevelopmentTokenVerifier(TokenVerifier):
    """Static local-only tokens. Refuse any non-loopback binding in Settings."""

    def __init__(self, tokens: dict[str, str] | None, resource_url: str):
        parsed = urlparse(resource_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Development tokens only accept loopback resource URLs")
        if parsed.username or parsed.password:
            raise ValueError("Development resource URLs must not contain embedded credentials")
        tokens = tokens or {}
        self._token_hashes = {
            hashlib.sha256(token.encode("utf-8")).hexdigest(): agent_id
            for agent_id, token in tokens.items()
        }
        self.resource_url = resource_url.rstrip("/")

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        agent_id = self._token_hashes.get(digest)
        if agent_id is None:
            return None
        return AccessToken(
            token=token,
            client_id=agent_id,
            subject=agent_id,
            scopes=["vortex:connect", "vortex:read", "vortex:message"],
            resource=self.resource_url,
        )


def _identity(agent_ids: tuple[str, ...] | set[str]) -> str:
    access = get_access_token()
    if access is None or access.client_id not in agent_ids:
        raise PermissionError("Authenticated Vortex agent identity required")
    return access.client_id


def _operator(agent_ids: tuple[str, ...] | set[str], operator_ids: tuple[str, ...] | set[str]) -> str:
    """Resolve the token identity and require it to be a configured operator."""
    agent_id = _identity(agent_ids)
    if agent_id not in operator_ids:
        raise PermissionError("Operator privileges required; this identity is not in VORTEX_OPERATOR_IDS")
    return agent_id


def create_server(store: VortexStore | None = None, settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    agent_ids = settings.agent_ids or AGENT_IDS
    operator_ids = tuple(settings.operator_ids)
    unknown_operators = [agent for agent in operator_ids if agent not in agent_ids]
    if unknown_operators:
        raise RuntimeError("Operator IDs must be configured agent IDs: " + ", ".join(unknown_operators))
    approval_policy = tuple(settings.approval_required_for)
    unknown_policy = [entry for entry in approval_policy if entry != "*" and entry not in agent_ids]
    if unknown_policy:
        raise RuntimeError("Approval policy must name configured agent IDs or '*': " + ", ".join(unknown_policy))
    if store is None:
        store = VortexStore(
            os.path.join(settings.data_dir, "vortex.sqlite3"),
            agents={agent: agent for agent in agent_ids},
            approval_required_for=approval_policy,
        )
    else:
        store.set_approval_policy(approval_policy)

    if settings.auth_mode == "development":
        issuer_url = _loopback_url(settings.host, settings.port)
        resource_url = issuer_url + "/mcp"
        verifier: TokenVerifier = DevelopmentTokenVerifier(settings.development_tokens, resource_url)
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                f"127.0.0.1:{settings.port}",
                f"localhost:{settings.port}",
                f"[::1]:{settings.port}",
            ],
            allowed_origins=[
                f"http://127.0.0.1:{settings.port}",
                f"http://localhost:{settings.port}",
                f"http://[::1]:{settings.port}",
            ],
        )
    else:
        issuer_url = settings.oauth_issuer
        resource_url = settings.public_url + "/mcp"
        verifier = IntrospectionTokenVerifier(
            introspection_url=settings.oauth_introspection_url,
            client_id=settings.oauth_client_id,
            client_secret=settings.oauth_client_secret,
            issuer=settings.oauth_issuer,
            resource_url=resource_url,
            agent_client_ids=settings.agent_client_ids,
            agent_ids=agent_ids,
            ca_bundle=settings.oauth_ca_bundle,
        )
        hostnames = list(settings.allowed_hosts)
        origins = list(settings.allowed_origins)
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=hostnames,
            allowed_origins=origins,
        )

    auth = AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=resource_url,
        validate_token_resource=True,
        client_registration_options=ClientRegistrationOptions(enabled=False),
        required_scopes=["vortex:connect", "vortex:read", "vortex:message"],
    )
    server = FastMCP(
        "Vortex",
        instructions=(
            "Agent identity is derived from a validated token, never a tool argument. "
            "Messages are queued for polling and cannot wake an inactive agent. Configured instances: "
            + ", ".join(agent_ids)
            + ". vortex_operator_* tools work only for identities listed in VORTEX_OPERATOR_IDS."
        ),
        token_verifier=verifier,
        auth=auth,
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=transport_security,
    )

    @server.tool()
    def vortex_register_agent() -> dict[str, Any]:
        """Register or refresh presence for this authenticated agent instance."""
        return store.register(_identity(agent_ids))

    @server.tool()
    def vortex_list_agents() -> list[dict[str, Any]]:
        """List configured agent IDs and presence; never returns credentials."""
        return store.list_agents(_identity(agent_ids))

    @server.tool()
    def vortex_send_message(
        recipient_id: str,
        body: str,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Queue a bounded message for another instance, optionally attached to an accessible task."""
        return store.send(_identity(agent_ids), recipient_id, body, task_id=task_id)

    @server.tool()
    def vortex_get_inbox(
        limit: int = 20,
        after_message_id: str | None = None,
        include_acked: bool = False,
    ) -> list[dict[str, Any]]:
        """Read inbox messages in stable pages; pass the last message ID to continue."""
        return store.inbox(
            _identity(agent_ids),
            limit=limit,
            include_acked=include_acked,
            after_message_id=after_message_id,
        )

    @server.tool()
    def vortex_ack_message(message_id: str) -> dict[str, Any]:
        """Acknowledge a message addressed to this authenticated instance."""
        return store.acknowledge(_identity(agent_ids), message_id)

    @server.tool()
    def vortex_ack_messages(message_ids: list[str]) -> list[dict[str, Any]]:
        """Atomically acknowledge 1-100 messages addressed to this instance."""
        return store.acknowledge_many(_identity(agent_ids), message_ids)

    @server.tool()
    def vortex_get_thread(message_id: str) -> list[dict[str, Any]]:
        """Read the complete message thread for a message involving this instance."""
        return store.thread(_identity(agent_ids), message_id)

    @server.tool()
    def vortex_reply_message(message_id: str, body: str) -> dict[str, Any]:
        """Reply in an existing thread to another participant."""
        sender = _identity(agent_ids)
        parent = store.message_context(sender, message_id)
        recipient = parent["recipient_id"] if parent["sender_id"] == sender else parent["sender_id"]
        return store.send(sender, recipient, body, reply_to=message_id)

    @server.tool()
    def vortex_create_task(
        goal: str,
        assigned_to: str | None = None,
        priority: int = 3,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
        requires_approval: bool = False,
    ) -> dict[str, Any]:
        """Create durable work; priority 1 is highest, and dependencies must complete before claim.
        Set requires_approval for risky work: it stays unclaimable until an operator approves it
        (server policy may also require approval regardless of this flag)."""
        return store.create_task(
            _identity(agent_ids),
            goal,
            assigned_to=assigned_to,
            priority=priority,
            depends_on=depends_on,
            idempotency_key=idempotency_key,
            requires_approval=requires_approval,
        )

    @server.tool()
    def vortex_dispatch_tasks(
        goal: str,
        recipients: list[str],
        priority: int = 3,
        depends_on: list[str] | None = None,
        idempotency_key: str | None = None,
        requires_approval: bool = False,
    ) -> list[dict[str, Any]]:
        """Create one durable task per recipient in a single atomic batch; requires_approval gates each."""
        return store.dispatch_tasks(
            _identity(agent_ids),
            goal,
            recipients,
            priority=priority,
            depends_on=depends_on,
            idempotency_key=idempotency_key,
            requires_approval=requires_approval,
        )

    @server.tool()
    def vortex_list_tasks(status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List tasks created by or assigned to this agent, ordered by priority."""
        return store.list_tasks(_identity(agent_ids), status=status, limit=limit)

    @server.tool()
    def vortex_get_task(task_id: str) -> dict[str, Any]:
        """Get a task and its dependency readiness; only the creator or assignee may read it."""
        return store.get_task(_identity(agent_ids), task_id)

    @server.tool()
    def vortex_get_task_events(task_id: str) -> list[dict[str, Any]]:
        """Read the append-only task lifecycle history for an accessible task."""
        return store.task_events(_identity(agent_ids), task_id)

    @server.tool()
    def vortex_claim_task(
        task_id: str,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        """Claim eligible work, resume blocked work, or reclaim an expired lease (30-3600 seconds)."""
        return store.claim_task(_identity(agent_ids), task_id, lease_seconds=lease_seconds)

    @server.tool()
    def vortex_update_task(
        task_id: str,
        status: str,
        blocked_reason: str | None = None,
        lease_seconds: int = 300,
    ) -> dict[str, Any]:
        """Report in_progress (renew lease) or blocked (requires a reason) for your assigned task."""
        return store.update_task(
            _identity(agent_ids),
            task_id,
            status,
            blocked_reason=blocked_reason,
            lease_seconds=lease_seconds,
        )

    @server.tool()
    def vortex_complete_task(task_id: str, result: str = "") -> dict[str, Any]:
        """Complete an in-progress task while its lease is active, recording the result."""
        return store.complete_task(_identity(agent_ids), task_id, result=result)

    @server.tool()
    def vortex_cancel_task(task_id: str, reason: str = "") -> dict[str, Any]:
        """Cancel a non-terminal task as its creator or assignee."""
        return store.cancel_task(_identity(agent_ids), task_id, reason=reason)

    @server.tool()
    def vortex_spine_survey(registry_path: str | None = None) -> dict[str, Any]:
        """Read validated organ status from the configured SPINE registry; returns no filesystem paths."""
        _identity(agent_ids)
        rows, fingerprint = survey_organs(registry_path)
        return {"organs": rows, "count": len(rows), "registry_sha256": fingerprint}

    @server.tool()
    def vortex_spine_plan(
        registry_path: str | None = None,
        create_tasks: bool = False,
    ) -> dict[str, Any]:
        """Preview read-only SPINE advisory tasks; create bounded local records only when explicitly opted in."""
        creator_id = _identity(agent_ids)
        rows, fingerprint = survey_organs(registry_path)
        goals = advisory_task_goals(rows)
        if not create_tasks:
            return {
                "organs": rows,
                "registry_sha256": fingerprint,
                "tasks_created": False,
                "planned_tasks": [{"organ_id": organ_id, "goal": goal} for organ_id, goal in goals],
            }
        tasks = []
        for organ_id, goal in goals:
            assignee_id = _spine_assignee(organ_id, agent_ids)
            task = store.create_task(
                creator_id,
                goal,
                assigned_to=assignee_id,
                priority=3,
                idempotency_key=f"spine:{fingerprint}:{organ_id}",
            )
            tasks.append({"organ_id": organ_id, "task_id": task["task_id"], "assigned_to": task["assigned_to"]})
        return {
            "organs": rows,
            "registry_sha256": fingerprint,
            "tasks_created": True,
            "tasks": tasks,
        }

    # --- Operator oversight: fleet-wide read and control ---------------------

    @server.tool()
    def vortex_operator_overview() -> dict[str, Any]:
        """Operator only: fleet snapshot with agent presence, active work, unread counts and expired leases."""
        return store.operator_overview(_operator(agent_ids, operator_ids))

    @server.tool()
    def vortex_operator_list_tasks(
        status: str | None = None,
        assigned_to: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Operator only: list any task in the fleet, filtered by status and/or assignee (max 200)."""
        return store.operator_list_tasks(
            _operator(agent_ids, operator_ids), status=status, assigned_to=assigned_to, limit=limit
        )

    @server.tool()
    def vortex_operator_get_task(task_id: str) -> dict[str, Any]:
        """Operator only: any task with its full lifecycle event history."""
        return store.operator_get_task(_operator(agent_ids, operator_ids), task_id)

    @server.tool()
    def vortex_operator_cancel_task(task_id: str, reason: str) -> dict[str, Any]:
        """Operator only: cancel any non-terminal task; a reason is required and audited."""
        return store.operator_cancel_task(_operator(agent_ids, operator_ids), task_id, reason)

    @server.tool()
    def vortex_operator_events(after_event_id: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        """Operator only: fleet-wide task event feed; pass the last event_id seen to continue."""
        return store.operator_events(
            _operator(agent_ids, operator_ids), after_event_id=after_event_id, limit=limit
        )

    @server.tool()
    def vortex_operator_list_pending_approvals(limit: int = 50) -> list[dict[str, Any]]:
        """Operator only: open tasks awaiting an approval decision, highest priority and oldest first."""
        return store.operator_list_pending_approvals(_operator(agent_ids, operator_ids), limit=limit)

    @server.tool()
    def vortex_operator_approve_task(task_id: str, note: str = "") -> dict[str, Any]:
        """Operator only: approve a pending task so it becomes claimable; the decision is audited."""
        return store.operator_approve_task(_operator(agent_ids, operator_ids), task_id, note=note)

    @server.tool()
    def vortex_operator_reject_task(task_id: str, reason: str) -> dict[str, Any]:
        """Operator only: reject a pending task; it is cancelled permanently. A reason is required."""
        return store.operator_reject_task(_operator(agent_ids, operator_ids), task_id, reason)

    return server


def _spine_assignee(organ_id: str, agent_ids: tuple[str, ...] | set[str]) -> str:
    preferred = {
        "citadel": "hermes-2",
        "o0": "hermes-2",
        "markus": "antigravity-1",
        "horizontal": "antigravity-1",
        "vorpal": "spark-1",
        "aih": "spark-1",
        "vortex-desktop": "hermes-1",
        "hermes": "hermes-1",
    }.get(organ_id, "hermes-2")
    return preferred if preferred in agent_ids else sorted(agent_ids)[0]


def run() -> None:
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    run()
