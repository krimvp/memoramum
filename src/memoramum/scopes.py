"""Scopes: the tree, chain resolution, and retrieval-time access checks.

Implements doc 01 §3 and doc 05 §4.1. The load-bearing rule is the
source-visibility invariant: a memory never surfaces to a principal who
could not see its source scope *at the time of the request* — so
membership is checked at read time, never cached at index time.
"""

from __future__ import annotations

import fnmatch
import json
from typing import Any

from .principals import Flow, Principal


class ScopeError(ValueError):
    pass


def subject_scope_id(principal: str) -> str:
    """'user:dana' → 'subject:user/dana' (the id convention of doc 01 §3.1)."""
    return "subject:" + principal.replace(":", "/", 1)


def create_scope(
    cur,
    *,
    scope_id: str,
    family: str,
    parent_scope_id: str | None = None,
    surface: str | None = None,
    external_ref: dict | None = None,
    trust_class: str = "internal_public",
) -> dict[str, Any]:
    return cur.execute(
        "INSERT INTO scopes (id, family, parent_scope_id, surface, external_ref, trust_class)"
        " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (scope_id, family, parent_scope_id, surface,
         json.dumps(external_ref) if external_ref is not None else None, trust_class),
    ).fetchone()


def get_scope(cur, scope_id: str) -> dict[str, Any] | None:
    return cur.execute("SELECT * FROM scopes WHERE id = %s", (scope_id,)).fetchone()


def ancestors(cur, scope_id: str) -> list[dict[str, Any]]:
    """The scope and its ancestors, narrowest first, up to the root."""
    return cur.execute(
        """
        WITH RECURSIVE up AS (
            SELECT s.*, 0 AS depth FROM scopes s WHERE s.id = %s
            UNION ALL
            SELECT p.*, up.depth + 1 FROM scopes p JOIN up ON p.id = up.parent_scope_id
        )
        SELECT * FROM up ORDER BY depth
        """,
        (scope_id,),
    ).fetchall()


def flow_project_scope(cur, container_scope_id: str | None) -> str | None:
    """The project container of a flow (ADR-0009/0010): the nearest container
    ancestor of `container` whose parent has family 'surface' — structural,
    never parse scope-id strings. None when no such ancestor exists (e.g. a
    'devsession' under surface:ide whose chain reaches no project)."""
    if not container_scope_id:
        return None
    anc = ancestors(cur, container_scope_id)
    by_id = {s["id"]: s for s in anc}
    for scope in anc:
        parent = by_id.get(scope["parent_scope_id"])
        if scope["family"] == "container" and parent is not None and parent["family"] == "surface":
            return scope["id"]
    return None


def modules_touched(cur, project_scope_id: str, paths) -> list[str]:
    """The module scopes touched by these repository paths (ADR-0010): match
    each path against the `module_paths` globs, most-specific (longest glob)
    wins per path, restricted to module scopes homed under the project scope.
    Ordered by first appearance, unique. Unmatched paths belong to no module
    — they fall through to the project scope (already in the chain), never to
    a wrong module (ADR-0010: fail-open to project)."""
    paths = [p for p in (paths or []) if p]
    if not paths or not project_scope_id:
        return []
    rows = cur.execute(
        """
        WITH RECURSIVE down AS (
            SELECT id FROM scopes WHERE id = %s
            UNION ALL
            SELECT s.id FROM scopes s JOIN down ON s.parent_scope_id = down.id
        )
        SELECT mp.module_scope_id, mp.glob FROM module_paths mp
        JOIN scopes s ON s.id = mp.module_scope_id AND s.family = 'module'
        WHERE mp.module_scope_id IN (SELECT id FROM down)
        """,
        (project_scope_id,),
    ).fetchall()
    ordered: list[str] = []
    for path in paths:
        best_module, best_len = None, -1
        for r in rows:
            if fnmatch.fnmatch(path, r["glob"]) and len(r["glob"]) > best_len:
                best_module, best_len = r["module_scope_id"], len(r["glob"])
        if best_module is not None and best_module not in ordered:
            ordered.append(best_module)
    return ordered


def _has_relation(cur, scope_ids: list[str], relation: str, principal: str) -> bool:
    if not scope_ids:
        return False
    row = cur.execute(
        "SELECT 1 FROM scope_relations WHERE scope_id = ANY(%s) AND relation = %s AND principal = %s LIMIT 1",
        (scope_ids, relation, principal),
    ).fetchone()
    return row is not None


def user_can_see(cur, user: str, scope_id: str) -> bool:
    """Would this user see content originating in this scope?

    Walks from the scope upward to the nearest scope that carries any
    member/owner tuples and evaluates there — a thread with no tuples of
    its own is governed by its channel. A `private` trust-class scope with
    no tuples fails closed (doc 07 §5).
    """
    for scope in ancestors(cur, scope_id):
        rows = cur.execute(
            "SELECT principal FROM scope_relations"
            " WHERE scope_id = %s AND relation IN ('member','owner')",
            (scope["id"],),
        ).fetchall()
        if rows:
            return any(r["principal"] == user for r in rows)
        if scope["trust_class"] == "private":
            return False
    return False


def readable(cur, principal: Principal, scope_id: str) -> bool:
    """doc 05 §4.1: readable(S) = enrolled(agent, S) ∧ (user is NULL ∨ member(user, S)).

    The intersection rule: an agent enrolled org-wide still cannot recall
    channel-scoped memories for a user who isn't in that channel.
    """
    scope = get_scope(cur, scope_id)
    if scope is None:
        return False

    # A principal's own agent scope is always its to read (doc 07 §5).
    if scope["family"] == "agent":
        return scope_id == principal.actor

    chain_up = [s["id"] for s in ancestors(cur, scope_id)]
    user = principal.effective_user

    if principal.kind == "system":
        return True
    if principal.kind == "agent":
        if not _has_relation(cur, chain_up, "reader_agent", principal.actor):
            return False
        if user is None:
            return True
        return user_can_see(cur, user, scope_id)
    # A user acting directly (review UI, DSAR) reads what they can see.
    return user is not None and user_can_see(cur, user, scope_id)


def writable(cur, principal: Principal, scope_id: str) -> bool:
    scope = get_scope(cur, scope_id)
    if scope is None:
        return False
    if scope["family"] == "agent":
        return scope_id == principal.actor
    if principal.kind == "system":
        return True
    chain_up = [s["id"] for s in ancestors(cur, scope_id)]
    if principal.kind == "agent":
        if not _has_relation(cur, chain_up, "writer_agent", principal.actor):
            return False
        user = principal.effective_user
        return user is None or user_can_see(cur, user, scope_id)
    # Direct user writes: to scopes they can see, or their own subject scope.
    user = principal.effective_user
    if user is None:
        return False
    if scope["family"] == "subject":
        return _has_relation(cur, [scope_id], "owner", user)
    return user_can_see(cur, user, scope_id)


def resolve_chain(cur, principal: Principal, flow: Flow) -> list[str]:
    """The ordered scope chain for a flow (doc 01 §3.3), already filtered by
    what the requesting principal may read. Narrower scopes come first —
    retrieval's scope_proximity weight keys off this order.

    Agents cannot request arbitrary scopes: the chain is computed from
    where they say they are, and every element is access-checked.
    """
    chain: list[str] = []
    project_scope_id = flow.project
    if flow.container:
        if get_scope(cur, flow.container) is None:
            raise ScopeError(f"unknown container scope {flow.container!r}")
        for scope in ancestors(cur, flow.container):
            if scope["family"] == "surface":
                continue  # structural node, not a memory home (doc 01 §3.3 example)
            chain.append(scope["id"])
        if project_scope_id is None:
            project_scope_id = flow_project_scope(cur, flow.container)

    # Module scopes touched by this flow (ADR-0009/0010): pulled into the
    # chain contextually — "modules touched by this MR/session" — exactly the
    # way participant subject scopes are. They sit immediately after the
    # container (narrower than the project, so scope_proximity ranks them
    # just inside it). Dev-time flows (container is a 'devsession', not under
    # the project) carry the project explicitly and get it slotted in after
    # the modules — no per-surface copy of project scopes (ADR-0012).
    if project_scope_id and flow.touched_paths:
        modules = [m for m in modules_touched(cur, project_scope_id, flow.touched_paths)
                   if m not in chain]
        at = 1 if flow.container else 0
        chain[at:at] = modules
        if project_scope_id not in chain:
            chain.insert(at + len(modules), project_scope_id)

    # Shared scopes the agent is *directly* enrolled in on another surface
    # (README scenario step 4: "repo → org + relevant shared scopes it is
    # enrolled in"; doc 01 §5: "Marge's scope chain includes the shared
    # scope"). Direct tuples only, cross-surface only: inherited org-wide
    # enrollment must not pull every descendant container into the chain —
    # that would defeat the doc 01 §3.3 isolation example — and same-surface
    # sibling containers stay out of each other's chains.
    if principal.kind == "agent":
        for row in cur.execute(
            "SELECT r.scope_id FROM scope_relations r JOIN scopes s ON s.id = r.scope_id"
            " WHERE r.principal = %s AND r.relation = 'reader_agent'"
            " AND s.family = 'container' AND s.trust_class <> 'private'"
            " AND s.surface IS NOT NULL AND s.surface IS DISTINCT FROM %s"
            " ORDER BY r.scope_id",
            (principal.actor, flow.surface),
        ).fetchall():
            if row["scope_id"] not in chain:
                chain.append(row["scope_id"])

    # Participants' subject scopes (policy-gated; readable() below enforces
    # that in P1 only the subject themself unlocks their subject scope).
    participants = list(flow.participants)
    user = principal.effective_user
    if user and user not in participants:
        participants.append(user)
    for p in participants:
        sid = subject_scope_id(p)
        if get_scope(cur, sid) is not None:
            chain.append(sid)

    # The agent's own private notebook, always last.
    if principal.kind == "agent" and get_scope(cur, principal.actor) is not None:
        chain.append(principal.actor)

    return [s for s in chain if readable(cur, principal, s)]


def membership_staleness_seconds(cur, surface: str | None) -> float | None:
    """Seconds since the surface's last membership-sync heartbeat
    (doc 07 §5), or None when there is nothing to measure: no surface in
    the flow, or a surface that has never synced — its membership is
    authored directly in the service and has no sync to go stale."""
    if not surface:
        return None
    row = cur.execute(
        "SELECT EXTRACT(epoch FROM now() - synced_at) AS age"
        " FROM membership_sync WHERE surface = %s",
        (surface,),
    ).fetchone()
    return float(row["age"]) if row else None


def synced_private(cur, scope_ids: list[str]) -> set[str]:
    """The subset whose visibility rests on synced surface membership AND
    whose trust class is private — exactly the scopes that fail closed
    beyond the doc 07 §5 staleness bound. Service-native families
    (subject, agent, org) have no surface and are never dropped."""
    if not scope_ids:
        return set()
    rows = cur.execute(
        "SELECT id FROM scopes WHERE id = ANY(%s)"
        " AND trust_class = 'private' AND surface IS NOT NULL",
        (scope_ids,),
    ).fetchall()
    return {r["id"] for r in rows}


def is_auditor(cur, principal: Principal, org_scope_id: str) -> bool:
    """as-of/history queries are an audit feature, not an agent feature
    (doc 05 §4.2): they require the auditor relation on the org scope."""
    return _has_relation(cur, [org_scope_id], "auditor", principal.actor)
