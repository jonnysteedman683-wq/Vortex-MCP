"""Create or update the Vortex schema in Postgres. Run with an admin connection, never as vortex_writer."""
from __future__ import annotations

import psycopg

from .db import check_schema_name, schema_sql


def apply(admin_url: str, schema: str = "forge") -> None:
    """Idempotently create ``schema`` and the Vortex tables in it."""
    check_schema_name(schema)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        with conn.transaction():
            conn.execute(f'SET LOCAL search_path TO "{schema}"')
            conn.execute(schema_sql("postgres.sql"))
