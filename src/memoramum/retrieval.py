"""Retrieval: hybrid candidates + the composite score of doc 04 §3.

    score(m) = w_rel · relevance(m)   # RRF-fused vector ⊕ lexical, normalized
             × R(m)                   # retention exp(-t/S), doc 03 §5
             × trust_weight(m)        # trust_score, floored by read-context policy
             × status_weight(m)       # invariant 1.2 · active 1.0 · staged 0.6
             × scope_proximity(m)     # narrower scope in chain beats broader

Access control happens BEFORE this module runs: callers pass the already
permission-filtered scope chain — never post-filter an over-fetched set
(doc 04 §3). Retrieval never mutates trust/confidence.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .config import Settings

_RRF_K = 60
_LEG_LIMIT = 50


def retention(row: dict[str, Any], settings: Settings, now: datetime | None = None) -> float:
    """R = exp(-t/S); invariants are exempt (R ≡ 1). Kind-specific time
    constants: episodic ~4× faster than semantic, procedural ~4× slower."""
    if row["status"] == "invariant":
        return 1.0
    now = now or datetime.now(timezone.utc)
    t_days = max((now - row["last_accessed_at"]).total_seconds() / 86400.0, 0.0)
    factor = settings.decay_kind_factor.get(row["kind"], 1.0)
    s_days = row["strength"] * settings.decay_base_days * factor
    return math.exp(-t_days / s_days)


_SENSITIVITIES = ("public", "internal", "confidential", "restricted")


def _candidate_filters(
    *,
    include_staged: bool,
    kinds: list[str] | None,
    subjects: list[str] | None,
    trust_floor: float,
    as_of: datetime | None,
    sensitivity_ceiling: str | None,
    deny_categories: list[str] | None,
) -> tuple[str, list]:
    statuses = ["active", "invariant"] + (["staged"] if include_staged else [])
    where = ["m.scope_id = ANY(%s)", "m.trust_score >= %s"]
    params: list = [None, trust_floor]  # scope chain patched in by caller
    if sensitivity_ceiling is not None and sensitivity_ceiling != "restricted":
        # The read-side attribute rules (doc 05 §4.2) filter candidates,
        # never post-filter an over-fetched set (doc 04 §3).
        allowed = list(_SENSITIVITIES[: _SENSITIVITIES.index(sensitivity_ceiling) + 1])
        where.append("m.sensitivity = ANY(%s)")
        params.append(allowed)
    if deny_categories:
        where.append("NOT (m.categories && %s)")
        params.append(list(deny_categories))
    if as_of is None:
        where.append("m.status = ANY(%s)")
        params.append(statuses)
        where.append("m.invalid_at IS NULL")
    else:
        # Historical belief query (audit-gated upstream): recorded by then,
        # and no lifecycle-ending event — supersession, deprecation,
        # archival, rejection — had happened yet. The event log is what
        # makes "what did we believe on date X" answerable (doc 03 §3).
        where.append("m.status <> 'tombstoned'")
        where.append("m.recorded_at <= %s")
        params.append(as_of)
        where.append("(m.invalid_at IS NULL OR m.invalid_at > %s)")
        params.append(as_of)
        where.append(
            "NOT EXISTS (SELECT 1 FROM memory_events e WHERE e.memory_id = m.id"
            " AND e.action IN ('SUPERSEDE','DEPRECATE','ARCHIVE','REJECT','TOMBSTONE','FORGET')"
            " AND e.at <= %s)"
        )
        params.append(as_of)
    if kinds:
        where.append("m.kind = ANY(%s)")
        params.append(kinds)
    if subjects:
        where.append("m.subject_ids && %s")
        params.append(subjects)
    return " AND ".join(where), params


def search(
    cur,
    *,
    scope_chain: list[str],
    query: str | None,
    query_embedding: list[float] | None,
    settings: Settings,
    kinds: list[str] | None = None,
    subjects: list[str] | None = None,
    include_staged: bool = True,
    trust_floor: float | None = None,
    as_of: datetime | None = None,
    sensitivity_ceiling: str | None = None,
    deny_categories: list[str] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Returns memory rows with a `score` key, best first."""
    if not scope_chain:
        return []
    floor = settings.default_trust_floor if trust_floor is None else trust_floor
    where, params = _candidate_filters(
        include_staged=include_staged, kinds=kinds, subjects=subjects,
        trust_floor=floor, as_of=as_of,
        sensitivity_ceiling=sensitivity_ceiling, deny_categories=deny_categories,
    )

    ranks: dict[str, dict[str, int]] = {}
    rows_by_id: dict[str, dict[str, Any]] = {}

    def leg(sql_extra: str, leg_name: str, extra_params: list) -> None:
        p = [scope_chain] + params[1:] + extra_params
        rows = cur.execute(
            f"SELECT m.* FROM memories m WHERE {where} {sql_extra} LIMIT {_LEG_LIMIT}", p
        ).fetchall()
        for rank, row in enumerate(rows):
            mid = str(row["id"])
            rows_by_id.setdefault(mid, row)
            ranks.setdefault(mid, {})[leg_name] = rank

    if query:
        leg(
            "AND m.content_tsv @@ websearch_to_tsquery('english', %s)"
            " ORDER BY ts_rank_cd(m.content_tsv, websearch_to_tsquery('english', %s)) DESC",
            "lexical",
            [query, query],
        )
        if query_embedding is not None:
            leg(
                "AND m.content_embedding IS NOT NULL"
                " ORDER BY m.content_embedding <=> %s::vector",
                "vector",
                [str(query_embedding)],
            )
    if not query or not ranks:
        # No retrieval focus (ambient block without one) or no hybrid hits:
        # fall back to the non-relevance factors over recent candidates.
        leg("ORDER BY m.last_accessed_at DESC", "recency", [])

    if not rows_by_id:
        return []

    rrf = {mid: sum(1.0 / (_RRF_K + r) for r in legs.values()) for mid, legs in ranks.items()}
    max_rrf = max(rrf.values())
    now = datetime.now(timezone.utc)
    scored = []
    for mid, row in rows_by_id.items():
        relevance = rrf[mid] / max_rrf
        proximity = settings.scope_proximity_base ** scope_chain.index(row["scope_id"])
        score = (
            relevance
            * retention(row, settings, now)
            * row["trust_score"]
            * settings.status_weights.get(row["status"], 1.0)
            * proximity
        )
        scored.append({**row, "score": score})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
