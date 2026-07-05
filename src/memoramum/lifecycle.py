"""Lifecycle rules (doc 03): reinforcement, staged→active promotion, and
the contradiction judge.

This module holds the *rules*; the service (write path, `memory_reinforce`)
and the consolidator (batch jobs, doc 07 §2) apply them and emit the
events. Judging whether two memories state conflicting facts is LLM-shaped
work; like the embedder, it hides behind a one-method interface with
deterministic local implementations for dev and tests. A real model judge
slots in behind the same seam.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

# Strength increments per reinforcement signal (doc 03 §5): useful
# retrieval +1, re-observation +2, explicit confirm +5. Reinforcement also
# resets t (last_accessed_at).
REINFORCEMENT = {"useful": 1.0, "re_observation": 2.0, "confirm": 5.0}

# Signals agents may send through memory_reinforce (doc 04 §1). The others
# are system-derived: re_observation on the write path, confirm by humans.
AGENT_SIGNALS = ("useful", "wrong")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------- the contradiction judge ----------

class Judge(Protocol):
    def judge(self, new_content: str, old_content: str) -> str:
        """'duplicate' | 'contradiction' | 'unrelated'."""
        ...


class ExactJudge:
    """Default: duplicates on normalized equality, never a contradiction.
    Without a model, write-time contradiction detection stays off; the
    'wrong' reinforcement signal and the review queue remain the paths in."""

    def judge(self, new_content: str, old_content: str) -> str:
        return "duplicate" if normalize(new_content) == normalize(old_content) else "unrelated"


class OverlapJudge:
    """Deterministic dev/test stand-in (the HashEmbedder of judges): two
    distinct statements about mostly the same tokens are treated as
    conflicting claims. No semantics — overlap only; do not deploy."""

    threshold = 0.5

    def judge(self, new_content: str, old_content: str) -> str:
        a, b = normalize(new_content), normalize(old_content)
        if a == b:
            return "duplicate"
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return "unrelated"
        overlap = len(ta & tb) / min(len(ta), len(tb))
        return "contradiction" if overlap >= self.threshold else "unrelated"


def make_judge(name: str) -> Judge:
    if name == "exact":
        return ExactJudge()
    if name == "overlap":
        return OverlapJudge()
    raise ValueError(f"unknown judge {name!r} (expected 'exact' or 'overlap')")


# ---------- supersede vs hold (doc 03 §3, invariant: staged input can
# ---------- never supersede active memories) ----------

def must_hold(new_status: str, new_trust: float, old: dict[str, Any]) -> bool:
    """True when a contradiction is held for review instead of auto-
    superseding: invariants only change by explicit privileged action, and
    staged/lower-trust claims cannot assassinate active memories
    (doc 06 §3)."""
    if old["status"] == "invariant":
        return True
    if old["status"] == "active":
        return new_status == "staged" or new_trust < old["trust_score"]
    return False


# ---------- the promotion rule (doc 03 §4) ----------

def promotion_due(cur, mem: dict[str, Any], settings, now: datetime | None = None) -> bool:
    """Default rule: promote on (re-observations ≥ 1) OR (useful
    retrievals ≥ 2) OR explicit confirm, with a floor of 3 days in staging
    for agent_observed memories from non-member authors. A memory with an
    unresolved contradiction never rule-promotes ("no contradiction" is
    part of every earned path)."""
    if mem["status"] != "staged":
        return False
    now = now or datetime.now(timezone.utc)

    unresolved = cur.execute(
        "SELECT 1 FROM contradiction_queue WHERE resolved_at IS NULL"
        " AND (challenger_id=%s OR contradicted_id=%s) LIMIT 1",
        (mem["id"], mem["id"]),
    ).fetchone()
    if unresolved:
        return False

    counts = {
        r["signal"]: r["n"]
        for r in cur.execute(
            "SELECT details->>'signal' AS signal, count(*) AS n FROM memory_events"
            " WHERE memory_id=%s AND action='REINFORCE' GROUP BY 1",
            (mem["id"],),
        ).fetchall()
    }
    confirmed = cur.execute(
        "SELECT 1 FROM memory_events WHERE memory_id=%s AND action='CONFIRM' LIMIT 1",
        (mem["id"],),
    ).fetchone()
    earned = (
        counts.get("re_observation", 0) >= settings.promote_reobservations
        or counts.get("useful", 0) >= settings.promote_useful_retrievals
        or confirmed is not None
    )
    if not earned and settings.promote_tenure_days is not None:
        # Unchallenged tenure (optional, off by default): N days staged
        # with ≥1 retrieval and no contradiction.
        earned = (
            mem["access_count"] >= 1
            and now - mem["recorded_at"] >= timedelta(days=settings.promote_tenure_days)
        )
    if not earned:
        return False
    if confirmed is None and _floor_applies(cur, mem):
        return now - mem["recorded_at"] >= timedelta(days=settings.promote_floor_days)
    return True


def _floor_applies(cur, mem: dict[str, Any]) -> bool:
    """agent_observed memories whose source authors are not members of the
    memory's scope (or an ancestor) wait out the staging floor."""
    prov = cur.execute(
        "SELECT origin_kind FROM memory_provenance WHERE memory_id=%s", (mem["id"],)
    ).fetchone()
    if prov is None or prov["origin_kind"] != "agent_observed":
        return False
    authors = [
        r["author"]
        for r in cur.execute(
            "SELECT DISTINCT e.author FROM memory_derivations d"
            " JOIN episodes e ON e.id = d.source_id"
            " WHERE d.memory_id=%s AND d.source_type='episode' AND e.author IS NOT NULL",
            (mem["id"],),
        ).fetchall()
    ]
    if not authors:
        return True  # unattributed sources: treat as non-member (cautious)
    member = cur.execute(
        """
        WITH RECURSIVE up AS (
            SELECT s.* FROM scopes s WHERE s.id = %s
            UNION ALL
            SELECT p.* FROM scopes p JOIN up ON p.id = up.parent_scope_id
        )
        SELECT 1 FROM scope_relations r JOIN up ON up.id = r.scope_id
        WHERE r.relation IN ('member','owner') AND r.principal = ANY(%s) LIMIT 1
        """,
        (mem["scope_id"], authors),
    ).fetchone()
    return member is None


# ---------- re-observation independence (doc 03 §4) ----------

def independent_evidence(cur, memory_id: str, new_episode_ids: list[str]) -> bool:
    """A re-observation counts only from an independent episode: a
    different author or a different day than the evidence already backing
    the memory."""
    if not new_episode_ids:
        return False
    known = cur.execute(
        "SELECT e.author, e.occurred_at::date AS day FROM memory_derivations d"
        " JOIN episodes e ON e.id = d.source_id"
        " WHERE d.memory_id=%s AND d.source_type='episode'",
        (memory_id,),
    ).fetchall()
    authors = {r["author"] for r in known}
    days = {r["day"] for r in known}
    fresh = cur.execute(
        "SELECT author, occurred_at::date AS day FROM episodes WHERE id = ANY(%s::uuid[])",
        (new_episode_ids,),
    ).fetchall()
    return any(r["author"] not in authors or r["day"] not in days for r in fresh)
