"""OAuth 2.1 resource-server token introspection for Vortex."""
from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

AGENT_IDS = {"hermes-1", "hermes-2", "antigravity-1", "spark-1"}
REQUIRED_SCOPES = {"vortex:connect", "vortex:read", "vortex:message"}


class IntrospectionTokenVerifier(TokenVerifier):
    """Verify opaque OAuth access tokens using RFC 7662 introspection.

    Keep this resource-server integration separate from the authorization server.
    Identity is mapped only from the trusted introspection response's `sub`, never
    from a client-supplied agent ID, header, or unvalidated JWT claim.
    """

    def __init__(
        self,
        *,
        introspection_url: str,
        client_id: str,
        client_secret: str,
        issuer: str,
        resource_url: str,
        identity_clients: Mapping[str, str],
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        for label, url in (("introspection_url", introspection_url), ("issuer", issuer), ("resource_url", resource_url)):
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"{label} must be an HTTPS URL without embedded credentials")
        if set(identity_clients) != AGENT_IDS or any(not client for client in identity_clients.values()):
            raise ValueError("Map exactly the four configured agent IDs to OAuth client IDs")
        if len(set(identity_clients.values())) != len(identity_clients):
            raise ValueError("Each configured agent must have a distinct OAuth client ID")
        self.introspection_url = introspection_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.issuer = issuer.rstrip("/")
        self.resource_url = resource_url.rstrip("/")
        self.client_agents = {client_id: agent for agent, client_id in identity_clients.items()}
        self.timeout = timeout
        self.transport = transport

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or len(token) > 8192:
            return None
        try:
            async with httpx.AsyncClient(
                auth=(self.client_id, self.client_secret),
                timeout=self.timeout,
                follow_redirects=False,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    self.introspection_url,
                    data={"token": token, "token_type_hint": "access_token"},
                    headers={"Accept": "application/json"},
                )
                response.raise_for_status()
                claims: Any = response.json()
        except (httpx.HTTPError, ValueError):
            return None
        if not isinstance(claims, dict) or claims.get("active") is not True:
            return None
        subject = claims.get("sub")
        authenticated_client = claims.get("client_id") or claims.get("azp")
        if not isinstance(subject, str) or not subject:
            return None
        agent_id = self.client_agents.get(authenticated_client) if isinstance(authenticated_client, str) else None
        if agent_id is None:
            return None
        if claims.get("iss", "").rstrip("/") != self.issuer:
            return None
        aud = claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if self.resource_url not in audiences:
            return None
        exp = claims.get("exp")
        if not isinstance(exp, (int, float)) or exp <= time.time():
            return None
        raw_scope = claims.get("scope", "")
        if not isinstance(raw_scope, str):
            return None
        scopes = set(raw_scope.split())
        if not REQUIRED_SCOPES.issubset(scopes):
            return None
        return AccessToken(
            token=token,
            client_id=authenticated_client,
            subject=subject,
            scopes=sorted(scopes),
            expires_at=int(exp),
            resource=self.resource_url,
        )
