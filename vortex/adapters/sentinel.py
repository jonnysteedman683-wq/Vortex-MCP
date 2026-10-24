"""Sentinel adapter: makes a Sentinel (Arcane Quantum Brain) server a Vortex agent.

    VORTEX_URL=http://127.0.0.1:8000/mcp
    VORTEX_TOKEN=...                        # this adapter's Vortex identity token
    SENTINEL_URL=http://127.0.0.1:3000
    # Optional, for signed-in actions: a dedicated Firebase user for the adapter.
    SENTINEL_FIREBASE_API_KEY=...           # the web apiKey from firebase-applet-config.json
    SENTINEL_REFRESH_TOKEN=...              # that user's refresh token (keep secret)
    SENTINEL_ADAPTER_ALLOW_HEALING=1        # opt in to execute_healing
    python -m vortex.adapters.sentinel

Actions:
    health, diagnostics, quota_status        public, read-only
    health_history, debate_telemetry         need the Firebase user above
    execute_healing {"action_type": "..."}   changes server settings; opt-in, and
                                             gate it with VORTEX_APPROVAL_REQUIRED_FOR

Sentinel verifies Firebase ID tokens, so the adapter exchanges the refresh token for
short-lived ID tokens via Firebase's secure token endpoint and caches them until
shortly before they expire. No service-account key is involved.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..sdk import VortexAgent

FIREBASE_TOKEN_URL = "https://securetoken.googleapis.com/v1/token"

PUBLIC_ENDPOINTS = {
    "health": "/api/health",
    "diagnostics": "/api/debug/diagnostics",
    "quota_status": "/api/debug/quota-status",
}
SIGNED_IN_ENDPOINTS = {
    "health_history": "/api/system/health-history",
    "debate_telemetry": "/api/debate/telemetry",
}


class SentinelError(RuntimeError):
    pass


def _http(method: str, url: str, *, data: bytes | None = None, headers: dict[str, str] | None = None,
          timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise SentinelError(f"{method} {urllib.parse.urlsplit(url).path} returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SentinelError(f"Unreachable: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except ValueError:
        return raw


class FirebaseIdTokens:
    """Refresh-token -> ID-token exchange with caching."""

    def __init__(self, api_key: str, refresh_token: str, token_url: str = FIREBASE_TOKEN_URL):
        if not api_key or not refresh_token:
            raise ValueError("Both SENTINEL_FIREBASE_API_KEY and SENTINEL_REFRESH_TOKEN are required")
        self.api_key = api_key
        self.refresh_token = refresh_token
        self.token_url = token_url
        self._id_token: str | None = None
        self._expires_at = 0.0

    def get(self) -> str:
        if self._id_token and time.time() < self._expires_at - 60:
            return self._id_token
        body = urllib.parse.urlencode({"grant_type": "refresh_token", "refresh_token": self.refresh_token})
        data = _http(
            "POST", f"{self.token_url}?key={urllib.parse.quote(self.api_key)}",
            data=body.encode("utf-8"), headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if not isinstance(data, dict) or not data.get("id_token"):
            raise SentinelError("Firebase token exchange returned no id_token")
        self._id_token = data["id_token"]
        self._expires_at = time.time() + float(data.get("expires_in", 3600))
        self.refresh_token = data.get("refresh_token", self.refresh_token)  # rotation, if any
        return self._id_token


class SentinelClient:
    def __init__(self, base_url: str, tokens: FirebaseIdTokens | None = None, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, signed_in: bool = False) -> Any:
        headers: dict[str, str] = {}
        if signed_in:
            if self.tokens is None:
                raise PermissionError(
                    "This action needs a signed-in Sentinel user: set SENTINEL_FIREBASE_API_KEY and SENTINEL_REFRESH_TOKEN"
                )
            headers["Authorization"] = f"Bearer {self.tokens.get()}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        return _http(method, self.base_url + path, data=data, headers=headers, timeout=self.timeout)


def build_agent(agent: VortexAgent, client: SentinelClient, allow_healing: bool = False) -> VortexAgent:
    for action, path in PUBLIC_ENDPOINTS.items():
        agent.action(action)(lambda args, _p=path: client.request("GET", _p))
    for action, path in SIGNED_IN_ENDPOINTS.items():
        agent.action(action)(lambda args, _p=path: client.request("GET", _p, signed_in=True))

    @agent.action("execute_healing")
    def execute_healing(args: dict[str, Any]) -> Any:
        if not allow_healing:
            raise PermissionError("execute_healing is disabled; set SENTINEL_ADAPTER_ALLOW_HEALING=1 to opt in")
        action_type = args.get("action_type")
        if not isinstance(action_type, str) or not action_type:
            raise ValueError("execute_healing needs 'action_type'")
        return client.request("POST", "/api/system/execute-healing", {"actionType": action_type}, signed_in=True)

    return agent


def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    vortex_token = os.environ.get("VORTEX_TOKEN", "").strip()
    if not vortex_token:
        raise SystemExit("Set VORTEX_TOKEN to this adapter's Vortex identity token")
    api_key = os.environ.get("SENTINEL_FIREBASE_API_KEY", "").strip()
    refresh = os.environ.get("SENTINEL_REFRESH_TOKEN", "").strip()
    tokens = FirebaseIdTokens(api_key, refresh) if api_key and refresh else None
    client = SentinelClient(os.environ.get("SENTINEL_URL", "http://127.0.0.1:3000"), tokens)
    agent = VortexAgent(
        os.environ.get("VORTEX_URL", "http://127.0.0.1:8000/mcp"), vortex_token,
        poll_seconds=float(os.environ.get("VORTEX_POLL_SECONDS", "10")),
    )
    build_agent(agent, client, allow_healing=os.environ.get("SENTINEL_ADAPTER_ALLOW_HEALING") == "1").run_forever()


if __name__ == "__main__":
    main()
