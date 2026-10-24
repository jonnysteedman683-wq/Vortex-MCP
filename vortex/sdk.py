"""Adapter SDK: let any system join the Vortex fleet as an agent in a few lines.

    from vortex.sdk import VortexAgent

    agent = VortexAgent("http://127.0.0.1:8000/mcp", token=os.environ["VORTEX_TOKEN"])

    @agent.action("restart_gateway")
    def restart(args):
        ...  # do the work
        return {"restarted": True}

    agent.run_forever()

Task goals addressed to the agent are JSON objects: {"action": "<name>", "args": {...}}.
For each ready task the agent claims it, runs the matching handler and completes the
task with the handler's result. A handler error (or an unknown action) marks the task
blocked with the reason, so it shows up for the operator instead of disappearing.
Pauses and approval gates are enforced by the server; a paused agent just idles.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("vortex.sdk")

MAX_RESULT_CHARS = 8000
MAX_REASON_CHARS = 1000

Handler = Callable[[dict[str, Any]], Any]
Caller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class VortexError(RuntimeError):
    """A Vortex tool call returned an error."""


def parse_goal(goal: str) -> tuple[str | None, dict[str, Any]]:
    """Return (action, args) from a task goal, or (None, {}) if it is not an action goal."""
    try:
        data = json.loads(goal)
    except (TypeError, ValueError):
        return None, {}
    if not isinstance(data, dict) or not isinstance(data.get("action"), str):
        return None, {}
    args = data.get("args", {})
    return data["action"], args if isinstance(args, dict) else {}


def make_goal(action: str, args: dict[str, Any] | None = None) -> str:
    """Build a goal string for vortex_create_task that a VortexAgent can execute."""
    return json.dumps({"action": action, "args": args or {}}, sort_keys=True)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 15] + "...[truncated]"


def _unwrap(result: Any) -> Any:
    """Turn an MCP CallToolResult into plain Python data."""
    if getattr(result, "isError", False):
        text = result.content[0].text if result.content else "Vortex tool error"
        raise VortexError(text)
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured["result"] if set(structured) == {"result"} else structured
    if result.content:
        text = result.content[0].text
        try:
            return json.loads(text)
        except ValueError:
            return text
    return None


def http_caller(url: str, token: str) -> Caller:
    """Default transport: one short-lived Streamable HTTP session per call (Vortex is stateless)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def call(tool: str, args: dict[str, Any]) -> Any:
        async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, args)
        # Unwrap outside the client's task groups: raising inside them would turn a
        # VortexError into an ExceptionGroup that callers cannot catch by type.
        return _unwrap(result)

    return call


class VortexAgent:
    def __init__(
        self,
        url: str | None = None,
        token: str | None = None,
        *,
        caller: Caller | None = None,
        poll_seconds: float = 10.0,
        lease_seconds: int = 300,
        max_tasks_per_cycle: int = 5,
    ):
        if caller is None:
            if not url or not token:
                raise ValueError("Provide url and token, or a caller")
            caller = http_caller(url, token)
        self._call = caller
        self.poll_seconds = poll_seconds
        self.lease_seconds = lease_seconds
        self.max_tasks_per_cycle = max_tasks_per_cycle
        self.handlers: dict[str, Handler] = {}
        self.agent_id: str | None = None

    def action(self, name: str) -> Callable[[Handler], Handler]:
        """Decorator registering a handler for {"action": name} goals."""
        def register(fn: Handler) -> Handler:
            self.handlers[name] = fn
            return fn
        return register

    async def call(self, tool: str, **args: Any) -> Any:
        return await self._call(tool, args)

    async def heartbeat(self) -> dict[str, Any]:
        info = await self.call("vortex_register_agent")
        self.agent_id = info.get("agent_id", self.agent_id)
        return info

    async def _run_handler(self, handler: Handler, args: dict[str, Any]) -> Any:
        result = handler(args)
        if inspect.isawaitable(result):
            result = await result
        return result

    async def process_task(self, task: dict[str, Any]) -> str:
        """Claim and execute one task. Returns 'completed', 'blocked' or 'skipped'."""
        task_id = task["task_id"]
        try:
            await self.call("vortex_claim_task", task_id=task_id, lease_seconds=self.lease_seconds)
        except VortexError as exc:
            log.info("Could not claim %s: %s", task_id, exc)
            return "skipped"
        action, args = parse_goal(task.get("goal", ""))
        handler = self.handlers.get(action) if action else None
        if handler is None:
            reason = (
                f"Unsupported action {action!r}; this agent handles: {sorted(self.handlers)}"
                if action else "Goal is not an action goal ({\"action\": ..., \"args\": {...}})"
            )
            await self.call("vortex_update_task", task_id=task_id, status="blocked",
                            blocked_reason=_truncate(reason, MAX_REASON_CHARS))
            return "blocked"
        try:
            result = await self._run_handler(handler, args)
        except Exception as exc:  # report every handler failure to the operator
            await self.call("vortex_update_task", task_id=task_id, status="blocked",
                            blocked_reason=_truncate(f"{type(exc).__name__}: {exc}", MAX_REASON_CHARS))
            return "blocked"
        text = result if isinstance(result, str) else json.dumps(result, sort_keys=True, default=str)
        await self.call("vortex_complete_task", task_id=task_id, result=_truncate(text, MAX_RESULT_CHARS))
        return "completed"

    async def run_once(self) -> dict[str, int]:
        """Heartbeat, then work through ready tasks assigned to (or open for) this agent."""
        counts = {"completed": 0, "blocked": 0, "skipped": 0}
        await self.heartbeat()
        tasks = await self.call("vortex_list_tasks", status="open", limit=self.max_tasks_per_cycle)
        for task in tasks:
            if not task.get("ready"):
                continue  # waiting on dependencies or operator approval
            if task.get("assigned_to") not in (None, self.agent_id):
                continue
            counts[await self.process_task(task)] += 1
        return counts

    async def run_async(self, stop: asyncio.Event | None = None) -> None:
        while stop is None or not stop.is_set():
            try:
                await self.run_once()
            except Exception:  # keep the adapter alive across transient failures
                log.exception("Vortex cycle failed; retrying in %ss", self.poll_seconds)
            await asyncio.sleep(self.poll_seconds)

    def run_forever(self) -> None:
        asyncio.run(self.run_async())
