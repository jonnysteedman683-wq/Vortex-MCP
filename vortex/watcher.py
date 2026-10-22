"""Fleet watcher: a small non-AI loop that alerts only when something needs attention.

    VORTEX_URL=http://127.0.0.1:8000/mcp
    VORTEX_OPERATOR_TOKEN=...            # token of an identity in VORTEX_OPERATOR_IDS
    VORTEX_ALERT_NTFY_TOPIC=my-fleet     # optional: push to phone via the ntfy app
    VORTEX_ALERT_WEBHOOK=https://...     # optional: Slack/Discord-style webhook
    VORTEX_ALERT_COMMAND="claude -p ..." # optional: run a command per alert batch
    python -m vortex.watcher

Alerts (each fires once per change, not on every poll):
  - a task starts waiting for operator approval
  - expired leases (stuck work) appear or increase
  - an agent goes from online to offline
  - a task becomes blocked (with its reason)
  - an agent is paused
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import subprocess
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .sdk import Caller, http_caller

log = logging.getLogger("vortex.watcher")


@dataclass(frozen=True)
class Alert:
    kind: str
    message: str
    key: str


@dataclass
class WatchState:
    pending: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    online: set[str] = field(default_factory=set)
    paused: set[str] = field(default_factory=set)
    expired_leases: int = 0
    primed: bool = False


def compute_alerts(
    state: WatchState,
    overview: dict[str, Any],
    pending_tasks: list[dict[str, Any]],
    blocked_tasks: list[dict[str, Any]],
) -> tuple[list[Alert], WatchState]:
    """Pure diff of the previous snapshot against the current one. The first call only
    primes the state (no alert storm on startup) except for work already waiting on you."""
    online = {a["agent_id"] for a in overview.get("agents", []) if a.get("status") == "online"}
    paused = set(overview.get("paused_agents", []))
    pending = {t["task_id"] for t in pending_tasks}
    blocked = {t["task_id"] for t in blocked_tasks}
    expired = int(overview.get("expired_leases", 0))
    new_state = WatchState(pending, blocked, online, paused, expired, primed=True)

    alerts: list[Alert] = []
    for task in pending_tasks:
        if task["task_id"] not in state.pending:
            who = task.get("assigned_to") or "any agent"
            alerts.append(Alert("approval", f"Approval needed for {who}: {task['goal'][:160]}",
                                f"approval:{task['task_id']}"))
    if not state.primed:
        return alerts, new_state

    if expired > state.expired_leases:
        alerts.append(Alert("lease", f"{expired} task lease(s) expired (stuck work)", f"lease:{expired}"))
    for agent_id in sorted(state.online - online):
        alerts.append(Alert("offline", f"Agent {agent_id} went offline", f"offline:{agent_id}"))
    for task in blocked_tasks:
        if task["task_id"] not in state.blocked:
            alerts.append(Alert("blocked",
                                f"Task blocked ({task.get('assigned_to')}): {task.get('blocked_reason') or 'no reason'}",
                                f"blocked:{task['task_id']}"))
    for agent_id in sorted(paused - state.paused):
        alerts.append(Alert("paused", f"Agent {agent_id} was paused", f"paused:{agent_id}"))
    return alerts, new_state


def _post(url: str, body: bytes, headers: dict[str, str]) -> None:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=10):
        pass


def notify(alerts: list[Alert], env: dict[str, str] | None = None) -> None:
    """Deliver an alert batch to every configured channel; one failing channel never blocks others."""
    if not alerts:
        return
    env = dict(os.environ) if env is None else env
    text = "\n".join(f"[{a.kind}] {a.message}" for a in alerts)
    log.warning("Vortex alerts:\n%s", text)
    topic = env.get("VORTEX_ALERT_NTFY_TOPIC", "").strip()
    if topic:
        server = env.get("VORTEX_ALERT_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
        try:
            _post(f"{server}/{topic}", text.encode("utf-8"), {"Title": "Vortex fleet alert"})
        except Exception:
            log.exception("ntfy delivery failed")
    webhook = env.get("VORTEX_ALERT_WEBHOOK", "").strip()
    if webhook:
        try:
            _post(webhook, json.dumps({"text": text, "content": text}).encode("utf-8"),
                  {"Content-Type": "application/json"})
        except Exception:
            log.exception("Webhook delivery failed")
    command = env.get("VORTEX_ALERT_COMMAND", "").strip()
    if command:
        try:
            # No shell: the command is split into argv; alert text arrives via env and stdin.
            subprocess.run(shlex.split(command), input=text, text=True, timeout=600, check=False,
                           env={**env, "VORTEX_ALERT_TEXT": text})
        except Exception:
            log.exception("Alert command failed")


async def poll_once(call: Caller, state: WatchState) -> tuple[list[Alert], WatchState]:
    overview = await call("vortex_operator_overview", {})
    pending = await call("vortex_operator_list_pending_approvals", {"limit": 200})
    blocked = await call("vortex_operator_list_tasks", {"status": "blocked", "limit": 200})
    return compute_alerts(state, overview, pending, blocked)


async def watch(call: Caller, interval: float = 60.0, stop: asyncio.Event | None = None) -> None:
    state = WatchState()
    while stop is None or not stop.is_set():
        try:
            alerts, state = await poll_once(call, state)
            notify(alerts)
        except Exception:
            log.exception("Watcher poll failed; will retry")
        await asyncio.sleep(interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    url = os.environ.get("VORTEX_URL", "http://127.0.0.1:8000/mcp")
    token = os.environ.get("VORTEX_OPERATOR_TOKEN", "").strip()
    if not token:
        raise SystemExit("Set VORTEX_OPERATOR_TOKEN to an operator identity's token")
    interval = float(os.environ.get("VORTEX_WATCH_INTERVAL", "60"))
    asyncio.run(watch(http_caller(url, token), interval=interval))


if __name__ == "__main__":
    main()
