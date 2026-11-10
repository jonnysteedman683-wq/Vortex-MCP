"""Build stores on SQLite (default) or on Postgres when VORTEX_TEST_DATABASE_URL is set."""
from __future__ import annotations

import atexit
import os
import unittest
import uuid
from pathlib import Path
from typing import Any

from vortex.store import VortexStore

PG_URL = os.environ.get("VORTEX_TEST_DATABASE_URL")

sqlite_only = unittest.skipIf(PG_URL, "SQLite-specific")


def drop_schema(url: str, schema: str) -> None:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


_created: list[str] = []


@atexit.register
def _drop_created() -> None:
    for schema in _created:
        drop_schema(PG_URL, schema)


def make_store(path: str | Path = ":memory:", **kwargs: Any) -> VortexStore:
    """A store at ``path`` on SQLite, or in a fresh throwaway schema on Postgres (``path`` ignored)."""
    if not PG_URL:
        return VortexStore(path, **kwargs)
    from vortex.migrate import apply

    schema = "t_" + uuid.uuid4().hex
    apply(PG_URL, schema)
    _created.append(schema)
    return VortexStore(PG_URL, schema=schema, **kwargs)
