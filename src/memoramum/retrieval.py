"""Retrieval: three gated legs, RRF-fused, then the composite of doc 04 §3.

    score(m) = relevance(m)^w_rel        # RRF-fused lexical ⊕ vector ⊕ literal
             × R(m)^w_ret                # retention exp(-t/S), doc 03 §5
             × trust_score(m)^w_trust    # floored by read-context policy
             × status_weight(m)^w_status # invariant 1.2 · active 1.0 · staged 0.6
             × scope_proximity(m)^w_prox # narrower scope in chain beats broader

Each leg admits only what clears its own *absolute* gate before fusion
(ADR-0016): RRF ranks candidates but cannot say "no", and normalizing by the
best hit would score the top row as perfectly relevant whatever it is. So a
focused read that admits nothing returns nothing — the empty answer is what
lets an agent say "I have nothing on that" instead of citing the least-bad
row. Only a query-less read (an ambient block with no focus, doc 04 §2.1)
falls back to recency ordering, where there is no relevance question to ask.

The weights are exponents supplied by the read policy (ADR-0018); 0 disables
a factor, 1 is the doc 04 §3 default. The gates are not weights: a distance
ceiling is a property of the embedding space, not of an agent.

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
# pgvector release that added iterative index scans; below it, a filtered
# HNSW walk stops at ef_search candidates however few survive the filter.
_ITERATIVE_SCAN_SINCE = (0, 8, 0)
_iterative_scan: bool | None = None


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
    statuses: list[str] | None = None,
) -> tuple[str, list]:
    statuses = statuses or ["active", "invariant"] + (["staged"] if include_staged else [])
    where = [
        "m.scope_id = ANY(%s)",
        "m.trust_score >= %s",
        # Quarantined lineage is excluded from ALL retrieval pending
        # review (doc 06 §3) — an exclusion joined at read time, not a
        # status: the doc 02 status enum stays what it is.
        "NOT EXISTS (SELECT 1 FROM quarantine_items qi"
        " WHERE qi.memory_id = m.id AND qi.resolved_at IS NULL)",
    ]
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


def _iterative_scan_available(cur) -> bool:
    """Whether this pgvector build offers iterative index scans. Answered
    once per process: it is a property of the installed extension, one
    Postgres backs the service (ADR-0004), and asking would otherwise cost
    a round trip on every read."""
    global _iterative_scan
    if _iterative_scan is None:
        row = cur.execute(
            "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
        ).fetchone()
        version = tuple(
            int(part) for part in (row["extversion"] if row else "0").split(".")[:3]
            if part.isdigit()
        )
        _iterative_scan = bool(row) and version >= _ITERATIVE_SCAN_SINCE
    return _iterative_scan


def _tune_scans(cur, settings: Settings) -> None:
    """Retrieval always filters — scope chain, status, trust floor — and an
    HNSW walk applies those filters *after* the index, so a selective chain
    can exhaust the walk before it fills K and the leg silently under-returns
    (doc 07 §3). Widen the search and, where the build offers it, let the
    scan iterate until enough rows survive. `SET LOCAL` scopes this to the
    surrounding transaction, so nothing leaks into the pooled connection.
    """
    cur.execute(f"SET LOCAL hnsw.ef_search = {int(settings.hnsw_ef_search)}")
    if _iterative_scan_available(cur):
        cur.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    cur.execute(
        "SET LOCAL pg_trgm.word_similarity_threshold ="
        f" {float(settings.word_similarity_threshold)}"
    )


def invariants(
    cur,
    *,
    scope_chain: list[str],
    settings: Settings,
    trust_floor: float | None = None,
    sensitivity_ceiling: str | None = None,
    deny_categories: list[str] | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """The pinned tier (doc 03 §1): invariants are *always* included in
    ambient recall for their scope, budget permitting, first. They bypass
    the relevance legs entirely — an org rule is delivered because it is in
    force, not because it matched the focus — but every read-side gate of
    doc 05 §4.2 still applies. Ordered by scope proximity, narrowest first.
    """
    if not scope_chain:
        return []
    floor = settings.default_trust_floor if trust_floor is None else trust_floor
    where, params = _candidate_filters(
        include_staged=False, kinds=None, subjects=None, trust_floor=floor,
        as_of=None, sensitivity_ceiling=sensitivity_ceiling,
        deny_categories=deny_categories, statuses=["invariant"],
    )
    rows = cur.execute(
        f"SELECT m.* FROM memories m WHERE {where}"
        f" ORDER BY array_position(%s::text[], m.scope_id), m.recorded_at DESC LIMIT {int(limit)}",
        [scope_chain] + params[1:] + [scope_chain],
    ).fetchall()
    return list(rows)


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
    weights: dict[str, float] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """Returns memory rows with a `score` key, best first. Empty when the
    query admits no candidate — that is an answer, not a failure."""
    if not scope_chain:
        return []
    floor = settings.default_trust_floor if trust_floor is None else trust_floor
    where, params = _candidate_filters(
        include_staged=include_staged, kinds=kinds, subjects=subjects,
        trust_floor=floor, as_of=as_of,
        sensitivity_ceiling=sensitivity_ceiling, deny_categories=deny_categories,
    )
    w = {**settings.score_weights, **(weights or {})}

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
        _tune_scans(cur, settings)
        # Lexical: stemmed prose. Its gate is the tsquery match itself.
        leg(
            "AND m.content_tsv @@ websearch_to_tsquery('english', %s)"
            " ORDER BY ts_rank_cd(m.content_tsv, websearch_to_tsquery('english', %s)) DESC",
            "lexical",
            [query, query],
        )
        # Literal: paths, symbol names, workspace and MR ids — tokens the
        # english parser folds into single lexemes (ADR-0017). Gated by the
        # word-similarity threshold set in _tune_scans, which long prose
        # queries essentially never clear.
        leg(
            "AND %s <%% m.content ORDER BY word_similarity(%s, m.content) DESC",
            "literal",
            [query, query],
        )
        if query_embedding is not None:
            # Vector: paraphrase and synonymy, gated by an absolute cosine
            # distance — being the nearest of a bad lot is not a match.
            leg(
                "AND m.content_embedding IS NOT NULL"
                " AND (m.content_embedding <=> %s::vector) <= %s"
                " ORDER BY m.content_embedding <=> %s::vector",
                "vector",
                [str(query_embedding), settings.vector_distance_ceiling,
                 str(query_embedding)],
            )
    else:
        # No retrieval focus at all (ambient block without one): there is no
        # relevance question to answer, so rank the scope chain's working
        # set by the non-relevance factors alone.
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
            relevance ** w["relevance"]
            * retention(row, settings, now) ** w["retention"]
            * float(row["trust_score"]) ** w["trust"]
            * settings.status_weights.get(row["status"], 1.0) ** w["status"]
            * proximity ** w["scope_proximity"]
        )
        scored.append({**row, "score": score})
    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]
