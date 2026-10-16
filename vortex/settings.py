"""Validated Vortex configuration; settings contain no secret defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_AGENT_IDS = ("hermes-1", "hermes-2", "antigravity-1", "spark-1")
AGENT_IDS = DEFAULT_AGENT_IDS


def _agent_env_name(agent_id: str, *, prefer_client: bool) -> str:
    suffix = agent_id.upper().replace("-", "_")
    prefix = "VORTEX_CLIENT" if prefer_client else "VORTEX_SUBJECT"
    return f"{prefix}_{suffix}"


def _parse_agent_ids() -> tuple[str, ...]:
    raw = os.getenv("VORTEX_AGENT_IDS", "").strip()
    if not raw:
        return AGENT_IDS
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not ids or any(not agent or agent.startswith("-") for agent in ids):
        raise RuntimeError("VORTEX_AGENT_IDS must contain one or more non-empty agent IDs")
    if len(set(ids)) != len(ids):
        raise RuntimeError("VORTEX_AGENT_IDS must not contain duplicate agent IDs")
    return ids


def _parse_operator_ids(agent_ids: tuple[str, ...]) -> tuple[str, ...]:
    """Operators are configured agent identities granted fleet-wide oversight.

    Empty by default: no identity has operator powers unless explicitly listed.
    """
    raw = os.getenv("VORTEX_OPERATOR_IDS", "").strip()
    if not raw:
        return ()
    ids = tuple(part.strip() for part in raw.split(",") if part.strip())
    if len(set(ids)) != len(ids):
        raise RuntimeError("VORTEX_OPERATOR_IDS must not contain duplicate agent IDs")
    unknown = [agent for agent in ids if agent not in agent_ids]
    if unknown:
        raise RuntimeError(
            "VORTEX_OPERATOR_IDS must name configured agent IDs; unknown: " + ", ".join(unknown)
        )
    return ids


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required setting {name} is missing")
    return value


def _https_url(name: str) -> str:
    value = _required(name)
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError(f"{name} must be an HTTPS URL without embedded credentials")
    return value.rstrip("/")


def _optional_ca_bundle(name: str) -> str | None:
    """Optional PEM bundle that augments the system trust store (e.g. an
    internal or Tailscale-signed CA). Must exist when set."""
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not os.path.isfile(value):
        raise RuntimeError(f"{name} does not point to an existing file: {value}")
    return value


@dataclass(frozen=True)
class Settings:
    auth_mode: str
    host: str
    port: int
    data_dir: str
    agent_ids: tuple[str, ...] = ()
    public_url: str | None = None
    oauth_issuer: str | None = None
    oauth_introspection_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    oauth_ca_bundle: str | None = None
    identity_clients: dict[str, str] | None = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    development_tokens: dict[str, str] | None = None
    operator_ids: tuple[str, ...] = ()

    @property
    def agent_client_ids(self) -> dict[str, str] | None:
        return self.identity_clients

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("VORTEX_AUTH_MODE", "development").strip().lower()
        host = os.getenv("VORTEX_HOST", "127.0.0.1").strip()
        port = int(os.getenv("VORTEX_PORT", "8000"))
        data_dir = os.getenv("VORTEX_DATA_DIR", ".vortex-data")
        agent_ids = _parse_agent_ids()
        operator_ids = _parse_operator_ids(agent_ids)
        if not 1 <= port <= 65535:
            raise RuntimeError("VORTEX_PORT must be between 1 and 65535")
        if mode == "development":
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise RuntimeError("Development auth is loopback-only; remote TLS/OAuth is not implemented")
            tokens = {
                agent: _required("VORTEX_TOKEN_" + agent.upper().replace("-", "_"))
                for agent in agent_ids
            }
            if len(set(tokens.values())) != len(tokens):
                raise RuntimeError("Development bearer tokens must be distinct per agent")
            return cls(
                mode, host, port, data_dir,
                agent_ids=agent_ids, development_tokens=tokens, operator_ids=operator_ids,
            )
        if mode == "oauth":
            identity_clients = {}
            for agent in agent_ids:
                env_name = _agent_env_name(agent, prefer_client=True)
                legacy_name = _agent_env_name(agent, prefer_client=False)
                client_id = os.getenv(env_name, "").strip()
                legacy_client_id = os.getenv(legacy_name, "").strip()
                if client_id and legacy_client_id and client_id != legacy_client_id:
                    raise RuntimeError(
                        f"Conflicting OAuth client mappings for {agent}: {env_name} and {legacy_name} differ"
                    )
                resolved_client_id = client_id or legacy_client_id
                if resolved_client_id:
                    identity_clients[agent] = resolved_client_id
                else:
                    raise RuntimeError(f"Required setting {env_name} or {legacy_name} is missing")
            return cls(
                auth_mode="oauth",
                host=host,
                port=port,
                data_dir=data_dir,
                agent_ids=agent_ids,
                public_url=_https_url("VORTEX_PUBLIC_URL"),
                oauth_issuer=_https_url("VORTEX_OAUTH_ISSUER"),
                oauth_introspection_url=_https_url("VORTEX_OAUTH_INTROSPECTION_URL"),
                oauth_client_id=_required("VORTEX_OAUTH_CLIENT_ID"),
                oauth_client_secret=_required("VORTEX_OAUTH_CLIENT_SECRET"),
                oauth_ca_bundle=_optional_ca_bundle("VORTEX_OAUTH_CA_BUNDLE"),
                identity_clients=identity_clients,
                operator_ids=operator_ids,
                allowed_hosts=tuple(
                    h.strip()
                    for h in _required("VORTEX_ALLOWED_HOSTS").split(",")
                    if h.strip()
                ),
                allowed_origins=tuple(
                    o.strip()
                    for o in _required("VORTEX_ALLOWED_ORIGINS").split(",")
                    if o.strip()
                ),
            )
        raise RuntimeError("VORTEX_AUTH_MODE must be 'development' or 'oauth'")
