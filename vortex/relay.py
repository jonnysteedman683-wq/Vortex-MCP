"""Copy the Vortex outbox onto the UniSync ledger, exactly once per row.

Each outbox row becomes a ``memory.write`` in scope ``forge`` with key ``outbox/<id>``, posted as the
``vortex`` watcher (the same pattern as LinX's relay). Before posting, the relay checks the ledger for
that key, so a crash between "ledger accepted it" and "marked sent" never produces a duplicate.

    VORTEX_STORE_URL=... UNISYNC_GATEWAY_URL=... UNISYNC_VORTEX_TOKEN=... python -m vortex relay
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any, Protocol

from .store import VortexStore

SCOPE = "forge"
SENDER = "vortex"
KEY = "outbox/{outbox_id}"

log = logging.getLogger("vortex.relay")


class LedgerLike(Protocol):
    def post(self, sender: str, kind: str, task_id: str = "", **body: Any) -> Any: ...

    def memory(self, scope: str = "shared") -> dict[str, Any]: ...


def relay_once(store: VortexStore, ledger: LedgerLike, *, batch: int = 100) -> int:
    """Post unsent rows in order; stop at the first failure, leaving it and later rows unsent."""
    rows = store.outbox_unsent(batch)
    if not rows:
        return 0
    already = ledger.memory(SCOPE)
    marked = 0
    for row in rows:
        key = KEY.format(outbox_id=row["outbox_id"])
        if key not in already:
            try:
                ledger.post(
                    SENDER, "memory.write", key=key, scope=SCOPE,
                    value={"kind": row["event_kind"], **row["payload"]},
                )
            except Exception as exc:
                exc.outbox_id = row["outbox_id"]  # type: ignore[attr-defined]
                raise
        store.outbox_mark_sent(row["outbox_id"])
        marked += 1
    return marked


def run(
    store: VortexStore,
    ledger: LedgerLike,
    *,
    interval: float = 2.0,
    max_backoff: float = 300.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Relay forever; back off exponentially after failures, capped at ``max_backoff``."""
    failures = 0
    while True:
        try:
            relay_once(store, ledger)
        except Exception as exc:  # noqa: BLE001 - any failure leaves rows unsent and is retried
            failures += 1
            wait = min(interval * 2**failures, max_backoff)
            # Never log payloads or tokens: the error class and outbox id are enough to act on.
            log.warning(
                "relay failed (%s) at outbox %s; retrying in %.0fs",
                type(exc).__name__, getattr(exc, "outbox_id", "?"), wait,
            )
            sleep(wait)
            continue
        failures = 0
        sleep(interval)


def main() -> int:
    from lestinxor.unisync_supabase import SupabaseLedger

    missing = [n for n in ("VORTEX_STORE_URL", "UNISYNC_GATEWAY_URL", "UNISYNC_VORTEX_TOKEN") if not os.environ.get(n)]
    if missing:
        print("missing environment: " + ", ".join(missing))
        return 2
    logging.basicConfig(level=logging.INFO)
    store = VortexStore(os.environ["VORTEX_STORE_URL"], schema=os.environ.get("VORTEX_SCHEMA", "forge"))
    ledger = SupabaseLedger(url=os.environ["UNISYNC_GATEWAY_URL"], key=os.environ["UNISYNC_VORTEX_TOKEN"])
    run(store, ledger)
    return 0
