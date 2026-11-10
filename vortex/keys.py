"""Per-agent API keys (``VORTEX_AUTH_MODE=apikey``): the bearer-token verifier and the ``keys`` command.

    python -m vortex keys add <agent>      issue a key; it is printed once and never stored
    python -m vortex keys revoke <agent>   revoke every active key of that agent
    python -m vortex keys list             agents, hash prefixes and dates; never a key or a full hash

A key is ``vtx_`` + 43 URL-safe characters. The store keeps only its SHA-256 hash. Nothing here logs,
prints or raises a key, a full hash or a connection string; ``keys add`` prints the new key exactly once.
"""
from __future__ import annotations

import hashlib
import logging
import math
import time
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .store import KEY_PATTERN

logger = logging.getLogger("vortex.keys")

SCOPES = ("vortex:connect", "vortex:read", "vortex:message")
MAX_TTL = 30.0  # a revoked key must stop working within 30 seconds

__all__ = ["KEY_PATTERN", "MAX_TTL", "SCOPES", "ApiKeyTokenVerifier", "main"]


class ApiKeyTokenVerifier(TokenVerifier):
    """Map ``Authorization: Bearer <key>`` to the agent that key was issued to.

    A token that is not shaped like a key is refused before the store is asked. A well-formed one is
    hashed and looked up by its hash, so raw keys are never compared. Only positive lookups are cached,
    for at most ``ttl`` seconds from the lookup (use does not extend it), so a revoked key stops working
    within ``ttl`` seconds even for an agent that is mid-session.
    """

    def __init__(
        self,
        store: Any,
        *,
        ttl: float = MAX_TTL,
        clock: Callable[[], float] = time.monotonic,
        resource_url: str | None = None,
    ) -> None:
        if not isinstance(ttl, int | float) or math.isnan(ttl) or not 0 <= ttl <= MAX_TTL:
            raise ValueError(f"ttl must be between 0 and {MAX_TTL:g} seconds")
        self._store = store
        self._ttl = float(ttl)
        self._clock = clock
        self.resource_url = resource_url.rstrip("/") if resource_url else None
        # sha256(key) -> (agent_id, time of the store lookup). Never holds a key.
        self._cache: dict[str, tuple[str, float]] = {}

    def _fresh(self, looked_up_at: float, now: float) -> bool:
        return 0 <= now - looked_up_at < self._ttl

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not KEY_PATTERN.fullmatch(token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        now = self._clock()
        cached = self._cache.get(digest)
        if cached is not None and self._fresh(cached[1], now):
            agent_id = cached[0]
        else:
            self._cache.pop(digest, None)
            try:
                agent_id = await anyio.to_thread.run_sync(self._store.agent_for_key, token)
            except ConnectionError:
                # Fail closed. Name neither the key nor the database.
                logger.warning("API key lookup failed: the store is unavailable")
                return None
            if agent_id is None:
                return None
            # Stamp with the time before the lookup, so the window is never longer than ttl.
            self._cache = {d: entry for d, entry in self._cache.items() if self._fresh(entry[1], now)}
            if self._ttl > 0:
                self._cache[digest] = (agent_id, now)
        return AccessToken(
            # Not the key: this object is kept on the request and has a repr.
            token="apikey:" + digest[:8],
            client_id=agent_id,
            subject=agent_id,
            scopes=list(SCOPES),
            resource=self.resource_url,
        )


USAGE = "usage: python -m vortex keys add <agent> | revoke <agent> | list"


def _open_store() -> Any:
    """The Postgres store named by VORTEX_STORE_URL and VORTEX_SCHEMA. Takes no instance lock, so it
    runs beside a live server."""
    from .settings import _parse_agent_ids, parse_store
    from .store import VortexStore

    url, schema = parse_store()
    if url is None:
        raise RuntimeError("VORTEX_STORE_URL is not set (a postgresql:// connection string)")
    return VortexStore(url, agents={agent: agent for agent in _parse_agent_ids()}, schema=schema)


def main(argv: list[str]) -> int:
    command = argv[0] if argv else None
    if (command, len(argv)) not in (("add", 2), ("revoke", 2), ("list", 1)):
        print(USAGE)
        return 2
    try:
        store = _open_store()
    except ConnectionError:
        print("database unavailable")
        return 1
    except RuntimeError as exc:  # settings and schema errors; none of them carries the connection string
        print(exc)
        return 2
    try:
        if command == "add":
            try:
                key = store.issue_key(argv[1])
            except ValueError:
                print("unknown agent: add it to VORTEX_AGENT_IDS first")
                return 1
            print(key)
            print(f"Store this now: the key for {argv[1]} is shown once and cannot be recovered.")
            return 0
        if command == "revoke":
            count = store.revoke_keys(argv[1])
            if not count:
                print(f"no active keys for {argv[1]}")
                return 1
            print(f"revoked {count} key(s) for {argv[1]}")
            return 0
        rows = store.list_keys()
        for row in rows:
            state = f"revoked {row['revoked_at']}" if row["revoked_at"] else "active"
            print(f"{row['agent_id']}  {row['hash_prefix']}  created {row['created_at']}  {state}")
        if not rows:
            print("no keys")
        return 0
    except ConnectionError:
        print("database unavailable")
        return 1
    finally:
        store.close()
