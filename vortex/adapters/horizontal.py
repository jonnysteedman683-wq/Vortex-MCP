"""HORIZONTAL adapter: makes the HORIZONTAL Sovereign gateway a Vortex agent.

It talks to the gateway on loopback (default http://127.0.0.1:8900) with the
gateway's operator token, so the gateway itself never needs to be exposed.

    VORTEX_URL=http://127.0.0.1:8000/mcp
    VORTEX_TOKEN=...                       # this adapter's Vortex identity token
    HORIZONTAL_GATEWAY_URL=http://127.0.0.1:8900
    CITADEL_AUTH_TOKEN=...                 # the gateway's operator token
    python -m vortex.adapters.horizontal

Actions (task goal {"action": ..., "args": {...}}; build with vortex.sdk.make_goal):
    status, health, cluster_status, techtree_status, diamond_status,
    circadian_status                 read-only GETs
    search        {"q": "..."}       read-only search
    command       {"command": "scan", "target": "...", ...}
        Runs a gateway /api/command. Only read-only / dry-run commands are allowed
        by default; opt more in with HORIZONTAL_ADAPTER_EXTRA_COMMANDS=a,b and gate
        them with VORTEX_APPROVAL_REQUIRED_FOR=<this agent's id> on the server.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..sdk import VortexAgent

# Verified read-only or dry-run in horizontal/sovereign/gateway.py.
DEFAULT_COMMANDS = frozenset({"scan", "diagnose", "improve", "self_refactor", "vector_search"})

STATUS_ENDPOINTS = {
    "status": "/api/status",
    "health": "/api/health",
    "cluster_status": "/api/cluster/status",
    "techtree_status": "/api/techtree/status",
    "diamond_status": "/api/diamond/status",
    "circadian_status": "/api/circadian/status",
}


class GatewayError(RuntimeError):
    pass


class HorizontalGateway:
    """Minimal stdlib client for the Sovereign gateway."""

    def __init__(self, base_url: str, token: str, timeout: float = 30.0):
        if not token:
            raise ValueError("The HORIZONTAL gateway token (CITADEL_AUTH_TOKEN) is required")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise GatewayError(f"Gateway {method} {path} returned {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise GatewayError(f"Gateway unreachable at {self.base_url}: {exc.reason}") from exc
        try:
            return json.loads(raw)
        except ValueError:
            return raw


def allowed_commands(env: dict[str, str] | None = None) -> frozenset[str]:
    env = os.environ if env is None else env
    extra = {c.strip() for c in env.get("HORIZONTAL_ADAPTER_EXTRA_COMMANDS", "").split(",") if c.strip()}
    return DEFAULT_COMMANDS | extra


def build_agent(agent: VortexAgent, gateway: HorizontalGateway, commands: frozenset[str]) -> VortexAgent:
    """Register HORIZONTAL actions on a VortexAgent."""
    for action, path in STATUS_ENDPOINTS.items():
        agent.action(action)(lambda args, _path=path: gateway.request("GET", _path))

    @agent.action("search")
    def search(args: dict[str, Any]) -> Any:
        query = str(args.get("q", "")).strip()
        if not query:
            raise ValueError("search needs a non-empty 'q'")
        return gateway.request("GET", "/api/search?" + urllib.parse.urlencode({"q": query}))

    @agent.action("command")
    def command(args: dict[str, Any]) -> Any:
        name = args.get("command")
        if name not in commands:
            raise PermissionError(
                f"Command {name!r} is not allowed by this adapter; allowed: {sorted(commands)}. "
                "Opt in with HORIZONTAL_ADAPTER_EXTRA_COMMANDS."
            )
        payload = {k: v for k, v in args.items() if k != "command"}
        payload["action"] = name
        return gateway.request("POST", "/api/command", payload)

    return agent


def main() -> None:
    import logging

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    vortex_url = os.environ.get("VORTEX_URL", "http://127.0.0.1:8000/mcp")
    vortex_token = os.environ.get("VORTEX_TOKEN", "").strip()
    if not vortex_token:
        raise SystemExit("Set VORTEX_TOKEN to this adapter's Vortex identity token")
    gateway = HorizontalGateway(
        os.environ.get("HORIZONTAL_GATEWAY_URL", "http://127.0.0.1:8900"),
        os.environ.get("CITADEL_AUTH_TOKEN", "").strip(),
    )
    agent = VortexAgent(vortex_url, vortex_token, poll_seconds=float(os.environ.get("VORTEX_POLL_SECONDS", "10")))
    build_agent(agent, gateway, allowed_commands()).run_forever()


if __name__ == "__main__":
    main()
