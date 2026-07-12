"""Deployment bootstrap: provision the principals your tokens name.

The store is default-deny: a bearer token (ADR-0014, ADR-0015) proves an
actor, but until that principal is enrolled somewhere every read resolves
an empty scope chain and every write is denied. This tool makes the two
configs agree — it reads the principals out of MEMORAMUM_API_TOKENS
(plus MEMORAMUM_SEED_USERS / MEMORAMUM_SEED_AGENTS for principals
without tokens, e.g. stdio-launched agents) and provisions, idempotently:

  the org scope        MEMORAMUM_ORG_SCOPE, and surface:ide under it
                       (the dev-time surface, ADR-0012)
  per user             org membership; subject:user/<id>, owned by the user
  per agent            the agent:<id> scope; org read enrollment;
                       read/write on every seeded user's subject scope;
                       a private devsession/<id> container the agent reads
                       and writes and the seeded users are members of —
                       the default MEMORAMUM_CONTAINER /
                       X-Memoramum-Container for that agent's sessions, so
                       memory_observe works out of the box (doc 04 §1)
  per project          MEMORAMUM_SEED_PROJECTS: a container under the org,
                       every seeded user a member, every seeded agent
                       enrolled reader+writer
  the sole user        (or MEMORAMUM_SEED_OWNER) becomes org owner +
                       auditor — the human who reviews staged queues,
                       confirms asks, and queries the event log

This is the personal-deployment shape: every seeded agent serves every
seeded user. Multi-team deployments should enroll narrowly through the
REST admin surface instead (doc 04 §5). Existing scopes and relations are
never modified — the tool only adds what is missing, as `system:seed`,
evented like any principal.

    MEMORAMUM_API_TOKENS='user:anton=…,agent:claude-code=…,agent:codex=…' \\
    MEMORAMUM_SEED_PROJECTS=project/memoramum \\
    memoramum-seed
"""

from __future__ import annotations

import json
import os

from .config import Settings, settings_from_env
from .db import make_pool
from .principals import Principal, PrincipalError, validate_principal
from .service import MemoryService

SEED = Principal("system:seed")


def _bare(principal: str) -> str:
    return principal.partition(":")[2]


def _kind_checked(principals, kind: str) -> tuple[str, ...]:
    out = []
    for p in principals:
        validate_principal(p)
        if not p.startswith(f"{kind}:"):
            raise PrincipalError(f"{p!r}: expected a {kind}:* principal")
        if p not in out:
            out.append(p)
    return tuple(out)


def seed(
    service: MemoryService,
    settings: Settings,
    *,
    users=(),
    agents=(),
    projects=(),
    owner: str | None = None,
) -> dict:
    """Provision scopes and enrollments for the named principals; returns
    a report of what was added. Safe to re-run: already-present scopes and
    relations are left untouched (and un-evented)."""
    users = _kind_checked(users, "user")
    agents = _kind_checked(agents, "agent")
    org = settings.org_scope_id
    created: list[str] = []
    enrolled: list[dict] = []

    def _exists(query: str, params: tuple) -> bool:
        with service.pool.connection() as conn:
            return conn.cursor().execute(query, params).fetchone() is not None

    def ensure_scope(scope_id: str, **kw) -> None:
        if _exists("SELECT 1 FROM scopes WHERE id=%s", (scope_id,)):
            return
        service.create_scope(SEED, scope_id=scope_id, **kw)
        created.append(scope_id)

    def ensure_relation(scope_id: str, relation: str, principal: str) -> None:
        if _exists(
            "SELECT 1 FROM scope_relations WHERE scope_id=%s AND relation=%s AND principal=%s",
            (scope_id, relation, principal),
        ):
            return
        service.set_relation(SEED, scope_id, relation, principal)
        enrolled.append({"scope": scope_id, "relation": relation, "principal": principal})

    ensure_scope(org, family="org")
    ensure_scope("surface:ide", family="surface", parent_scope_id=org, surface="ide")

    for user in users:
        ensure_relation(org, "member", user)
        subject = f"subject:user/{_bare(user)}"
        ensure_scope(subject, family="subject", parent_scope_id=org)
        ensure_relation(subject, "owner", user)

    owner = owner or (users[0] if len(users) == 1 else None)
    if owner is not None:
        if owner not in users:
            raise PrincipalError(f"owner {owner!r} is not among the seeded users")
        ensure_relation(org, "owner", owner)
        ensure_relation(org, "auditor", owner)

    for project in projects:
        ensure_scope(project, family="container", parent_scope_id=org)
        for user in users:
            ensure_relation(project, "member", user)

    for agent in agents:
        ensure_scope(agent, family="agent", parent_scope_id=org)
        ensure_relation(org, "reader_agent", agent)
        devsession = f"devsession/{_bare(agent)}"
        ensure_scope(devsession, family="container", parent_scope_id="surface:ide",
                     surface="ide", trust_class="private")
        for scope in (devsession, *projects):
            ensure_relation(scope, "reader_agent", agent)
            ensure_relation(scope, "writer_agent", agent)
        # The devsession is the developer's own session (ADR-0012): the
        # seeded users are its members, or the doc 05 §4.1 intersection
        # would make the private container invisible to the very user the
        # agent acts for.
        for user in users:
            ensure_relation(devsession, "member", user)
        for user in users:
            subject = f"subject:user/{_bare(user)}"
            ensure_relation(subject, "reader_agent", agent)
            ensure_relation(subject, "writer_agent", agent)

    return {
        "org": org,
        "users": list(users),
        "agents": list(agents),
        "owner": owner,
        "created_scopes": created,
        "added_relations": enrolled,
    }


def _split_env(name: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in os.environ.get(name, "").split(",") if p.strip())


def main() -> None:
    settings = settings_from_env()
    token_principals = [principal for principal, _ in settings.api_tokens]
    users = [p for p in token_principals if p.startswith("user:")]
    agents = [p for p in token_principals if p.startswith("agent:")]
    users += [u for u in _split_env("MEMORAMUM_SEED_USERS") if u not in users]
    agents += [a for a in _split_env("MEMORAMUM_SEED_AGENTS") if a not in agents]
    if not users and not agents:
        raise SystemExit(
            "nothing to seed: no user:*/agent:* principals in MEMORAMUM_API_TOKENS, "
            "MEMORAMUM_SEED_USERS or MEMORAMUM_SEED_AGENTS"
        )
    service = MemoryService(make_pool(settings.database_url), settings)
    report = seed(
        service, settings, users=users, agents=agents,
        projects=_split_env("MEMORAMUM_SEED_PROJECTS"),
        owner=os.environ.get("MEMORAMUM_SEED_OWNER") or None,
    )
    print(json.dumps(report, indent=2, default=str))
