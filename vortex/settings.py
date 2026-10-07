"""Validated Vortex configuration; settings contain no secret defaults."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

AGENT_IDS = ("hermes-1", "hermes-2", "antigravity-1", "spark-1")


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


@dataclass(frozen=True)
class Settings:
    auth_mode: str
    host: str
    port: int
    data_dir: str
    public_url: str | None = None
    oauth_issuer: str | None = None
    oauth_introspection_url: str | None = None
    oauth_client_id: str | None = None
    oauth_client_secret: str | None = None
    identity_clients: dict[str, str] | None = None
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()
    development_tokens: dict[str, str] | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("VORTEX_AUTH_MODE", "development").strip().lower()
        host = os.getenv("VORTEX_HOST", "127.0.0.1").strip()
        port = int(os.getenv("VORTEX_PORT", "8000"))
        data_dir = os.getenv("VORTEX_DATA_DIR", ".vortex-data")
        if not 1 <= port <= 65535:
            raise RuntimeError("VORTEX_PORT must be between 1 and 65535")
        if mode == "development":
            if host not in ("127.0.0.1", "localhost", "::1"):
                raise RuntimeError("Development auth is loopback-only; remote TLS/OAuth is not implemented")
            tokens = {
                agent: _required("VORTEX_TOKEN_" + agent.upper().replace("-", "_"))
                for agent in AGENT_IDS
            }
            if len(set(tokens.values())) != len(tokens):
                raise RuntimeError("Development bearer tokens must be distinct per agent")
            return cls(mode, host, port, data_dir, development_tokens=tokens)
        raise RuntimeError("OAuth mode is not enabled until an authorization provider is selected and compatibility is verified")
