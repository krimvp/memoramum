"""Connection pool and migration runner.

One Postgres (ADR-0004): memories, episodes, events, scopes, and relation
tuples live in a single transactional store, so a write + its provenance +
its event commit atomically.
"""

from __future__ import annotations

import importlib.resources
import sys

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(database_url: str) -> ConnectionPool:
    return ConnectionPool(
        database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=True,
    )


def migrate(database_url: str) -> list[str]:
    """Apply pending SQL migrations in filename order; returns those applied."""
    files = sorted(
        p
        for p in importlib.resources.files("memoramum.migrations").iterdir()
        if p.name.endswith(".sql")
    )
    applied: list[str] = []
    with psycopg.connect(database_url) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            " name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations").fetchall()}
        for f in files:
            if f.name in done:
                continue
            conn.execute(f.read_text())
            conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
            applied.append(f.name)
        conn.commit()
    return applied


def main() -> None:
    from .config import settings_from_env

    url = sys.argv[1] if len(sys.argv) > 1 else settings_from_env().database_url
    applied = migrate(url)
    print(f"applied: {applied or 'nothing (up to date)'}")
