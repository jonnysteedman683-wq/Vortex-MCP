"""Vortex Streamable HTTP MCP service for a fixed set of agent instances."""
from __future__ import annotations

import hashlib
import hmac
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
        for expected, agent_id in self._token_hashes.items():
            if hmac.compare_digest(digest, expected):
                return AccessToken(
                    token=token,
                    client_id=agent_id,
                    subject=agent_id,
                    scopes=["vortex:connect", "vortex:read", "vortex:message"],
                    resource=self.resource_url,
                )
        return None


def _identity(agent_ids: tuple[str, ...] | set[str]) -> str:
    access = get_access_token()
    if access is None or access.client_id not in agent_ids:
        raise PermissionError("Authenticated Vortex agent identity required")
    return access.client_id


def create_server(store: VortexStore | None = None, settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    agent_ids = settings.agent_ids or AGENT_IDS
    store = store or VortexStore(os.path.join(settings.data_dir, "vortex.sqlite3"), agents={agent: agent for agent in agent_ids})

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
            identity_clients=settings.identity_clients,
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
    def vortex_send_message(recipient_id: str, body: str) -> dict[str, Any]:
        """Queue a bounded plain-text message for another configured instance."""
        return store.send(_identity(agent_ids), recipient_id, body)

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
        thread = store.thread(sender, message_id)
        parent = next(message for message in thread if message["message_id"] == message_id)
        recipient = parent["recipient_id"] if parent["sender_id"] == sender else parent["sender_id"]
        return store.send(sender, recipient, body, reply_to=message_id)

    return server


def run() -> None:
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    run()
