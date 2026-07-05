"""Integration fixtures: a real Postgres (pgvector) seeded with the
worked-scenario world from README.md — Sage in #deploys, Marge on MR !482,
dana, li, and the acme org."""

from __future__ import annotations

import os

import psycopg
import pytest

from memoramum.config import Settings
from memoramum.db import make_pool, migrate
from memoramum.principals import Flow, Principal
from memoramum.service import MemoryService

TEST_DB = os.environ.get(
    "MEMORAMUM_TEST_DATABASE_URL",
    "postgresql://memoramum:memoramum@127.0.0.1:5432/memoramum_test",
)

SYSTEM = Principal("system:test")
SAGE_FOR_DANA = Principal("agent:sage", "user:dana")
SAGE_FOR_LI = Principal("agent:sage", "user:li")
SAGE_FOR_EVE = Principal("agent:sage", "user:eve")
MARGE_FOR_DANA = Principal("agent:marge", "user:dana")
ADMIN = Principal("user:admin")
DANA = Principal("user:dana")

DEPLOYS_FLOW = Flow(surface="slack", container="channel/C0DEP",
                    participants=("user:dana", "user:li"), session_id="sess-1")
MR_FLOW = Flow(surface="gitlab", container="mr/482",
               participants=("user:dana",), session_id="sess-2")


@pytest.fixture(scope="session")
def pool():
    with psycopg.connect(TEST_DB, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
    migrate(TEST_DB)
    pool = make_pool(TEST_DB)
    yield pool
    pool.close()


@pytest.fixture(scope="session")
def svc(pool):
    service = MemoryService(pool, Settings(database_url=TEST_DB, embedder="hash"))
    _seed(service)
    return service


def _seed(svc: MemoryService) -> None:
    s = svc.create_scope
    s(SYSTEM, scope_id="org:acme", family="org")
    s(SYSTEM, scope_id="surface:slack", family="surface", parent_scope_id="org:acme", surface="slack")
    s(SYSTEM, scope_id="workspace/T024B", family="container", parent_scope_id="surface:slack",
      surface="slack", external_ref={"team": "T024B"})
    s(SYSTEM, scope_id="channel/C0DEP", family="container", parent_scope_id="workspace/T024B",
      surface="slack", external_ref={"channel": "C0DEP", "name": "#deploys"})
    s(SYSTEM, scope_id="thread/1709481600.123", family="container",
      parent_scope_id="channel/C0DEP", surface="slack")
    s(SYSTEM, scope_id="dm/D111", family="container", parent_scope_id="workspace/T024B",
      surface="slack", trust_class="private")
    s(SYSTEM, scope_id="surface:gitlab", family="surface", parent_scope_id="org:acme", surface="gitlab")
    s(SYSTEM, scope_id="project/platform-api", family="container",
      parent_scope_id="surface:gitlab", surface="gitlab")
    s(SYSTEM, scope_id="mr/482", family="container", parent_scope_id="project/platform-api",
      surface="gitlab")
    s(SYSTEM, scope_id="subject:user/dana", family="subject", parent_scope_id="org:acme")
    s(SYSTEM, scope_id="agent:sage", family="agent", parent_scope_id="org:acme")
    s(SYSTEM, scope_id="agent:marge", family="agent", parent_scope_id="org:acme")

    r = svc.set_relation
    for user in ("user:dana", "user:li", "user:eve", "user:admin", "user:mr-author"):
        r(SYSTEM, "org:acme", "member", user)
        r(SYSTEM, "workspace/T024B", "member", user)
    for user in ("user:dana", "user:li"):
        r(SYSTEM, "channel/C0DEP", "member", user)
    r(SYSTEM, "dm/D111", "member", "user:dana")

    # Sage works #deploys; Marge is enrolled org-wide (read-only), never on Slack containers.
    r(SYSTEM, "channel/C0DEP", "reader_agent", "agent:sage")
    r(SYSTEM, "channel/C0DEP", "writer_agent", "agent:sage")
    r(SYSTEM, "workspace/T024B", "reader_agent", "agent:sage")
    # The promotion target must be on the writable set (doc 04 §1): sage
    # may address workspace-level scopes, humans still confirm crossings.
    r(SYSTEM, "workspace/T024B", "writer_agent", "agent:sage")
    r(SYSTEM, "subject:user/dana", "reader_agent", "agent:sage")
    r(SYSTEM, "subject:user/dana", "writer_agent", "agent:sage")
    r(SYSTEM, "org:acme", "reader_agent", "agent:marge")
    r(SYSTEM, "org:acme", "reader_agent", "agent:sage")

    r(SYSTEM, "subject:user/dana", "owner", "user:dana")
    r(SYSTEM, "org:acme", "auditor", "user:admin")
    r(SYSTEM, "org:acme", "owner", "user:root")   # the org admin (policy administration, doc 05 §5)
