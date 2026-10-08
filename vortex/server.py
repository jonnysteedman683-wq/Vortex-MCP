"""Vortex Streamable HTTP MCP service for a fixed set of agent instances."""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Any

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


class DevelopmentTokenVerifier(TokenVerifier):
    """Static local-only tokens. Refuse any non-loopback binding in Settings."""

    def __init__(self, tokens: dict[str, str] | None):
        tokens = tokens or {}
        self._token_hashes = {
            hashlib.sha256(token.encode("utf-8")).hexdigest(): agent_id
            for agent_id, token in tokens.items()
        }

    async def verify_token(self, token: str) -> AccessToken | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for expected, agent_id in self._token_hashes.items():
            if hmac.compare_digest(digest, expected):
                return AccessToken(
                    token=token,
                    client_id=agent_id,
                    subject=agent_id,
                    scopes=["vortex:connect", "vortex:read", "vortex:message"],
                    resource=f"http://127.0.0.1:{os.getenv('VORTEX_PORT', '8000')}/mcp",
                )
        return None


def _identity() -> str:
    access = get_access_token()
    if access is None or access.client_id not in AGENT_IDS:
        raise PermissionError("Authenticated Vortex agent identity required")
    return access.client_id


def create_server(store: VortexStore | None = None, settings: Settings | None = None) -> FastMCP:
    settings = settings or Settings.from_env()
    store = store or VortexStore(os.path.join(settings.data_dir, "vortex.sqlite3"))

    if settings.auth_mode == "development":
        issuer_url = f"http://127.0.0.1:{settings.port}"
        resource_url = issuer_url + "/mcp"
        verifier: TokenVerifier = DevelopmentTokenVerifier(settings.development_tokens)
        transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:8000", "localhost:8000", "[::1]:8000"],
            allowed_origins=["http://127.0.0.1:8000", "http://localhost:8000"],
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
            + ", ".join(AGENT_IDS)
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
        return store.register(_identity())

    @server.tool()
    def vortex_list_agents() -> list[dict[str, Any]]:
        """List configured agent IDs and presence; never returns credentials."""
        return store.list_agents(_identity())

    @server.tool()
    def vortex_send_message(recipient_id: str, body: str) -> dict[str, Any]:
        """Queue a bounded plain-text message for another configured instance."""
        return store.send(_identity(), recipient_id, body)

    @server.tool()
    def vortex_get_inbox(limit: int = 20) -> list[dict[str, Any]]:
        """Read pending messages addressed to this authenticated instance."""
        return store.inbox(_identity(), limit=limit)

    @server.tool()
    def vortex_ack_message(message_id: str) -> dict[str, Any]:
        """Acknowledge a message addressed to this authenticated instance."""
        return store.acknowledge(_identity(), message_id)

    @server.tool()
    def vortex_reply_message(message_id: str, body: str) -> dict[str, Any]:
        """Reply in an existing thread to another participant."""
        sender = _identity()
        thread = store.thread(sender, message_id)
        parent = next(message for message in thread if message["message_id"] == message_id)
        recipient = parent["recipient_id"] if parent["sender_id"] == sender else parent["sender_id"]
        return store.send(sender, recipient, body, reply_to=message_id)

    return server


def run() -> None:
    create_server().run(transport="streamable-http")


if __name__ == "__main__":
    run()
