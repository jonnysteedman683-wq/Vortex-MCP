"""Create or update the Vortex schema in Postgres. Run with an admin connection, never as vortex_writer.

    VORTEX_MIGRATE_URL=<admin connection> [VORTEX_SCHEMA=forge] python -m vortex migrate
"""
from __future__ import annotations

import os

from .db import check_schema_name, schema_sql


def apply(admin_url: str, schema: str = "forge") -> None:
    """Idempotently create ``schema``, the Vortex tables in it, and the vortex_writer grants."""
    import psycopg

    check_schema_name(schema)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        with conn.transaction():
            conn.execute(f'SET LOCAL search_path TO "{schema}"')
            conn.execute(schema_sql("postgres.sql"))
            conn.execute(schema_sql("postgres_roles.sql").replace("{schema}", schema))


def main() -> int:
    url = os.environ.get("VORTEX_MIGRATE_URL")
    if not url:
        print("VORTEX_MIGRATE_URL is not set (an admin connection string)")
        return 2
    schema = os.environ.get("VORTEX_SCHEMA", "forge")
    apply(url, schema)
    print(f"migrated schema {schema}")
    return 0
