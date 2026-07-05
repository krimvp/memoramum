"""The doc 07 §4 "metrics that matter", computed from the store.

No second instrumentation pipeline: the event log already records every
read, write, and policy decision transactionally (ADR-0004, ADR-0006),
so the operational metrics are *queries*, not counters — the numbers an
admin sees are derived from the same rows an auditor would replay
(ADR-0008). One consequence is worth naming: event-log lag is
structurally zero here, because events commit in the same transaction
as the write they record; the field is reported anyway so dashboards
built against this shape survive a deployment that batches.

Each family below is one dict in the snapshot, in doc 07 §4's order:
retrieval quality, policy, lifecycle, audit/compliance, cost. Ratios are
NULL (None) when their denominator is empty — "no data" and "0.0" are
different findings on a dashboard.
"""

from __future__ import annotations

from typing import Any

from . import erasure
from .config import Settings

# Doc 06 §2 / doc 07 §4: erasure SLO — complete < 72 h.
ERASURE_SLO_SECONDS = 72 * 3600

WINDOW = "now() - make_interval(days => %s)"


def snapshot(cur, settings: Settings, *, days: int = 30) -> dict[str, Any]:
    """One point-in-time reading of every doc 07 §4 metric family.

    `days` bounds the rate-like metrics (decisions, promotions, reads);
    population metrics (tier counts, open queues) are always current.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    return {
        "window_days": days,
        "retrieval_quality": _retrieval_quality(cur, days),
        "policy": _policy(cur, days),
        "lifecycle": _lifecycle(cur, days),
        "audit": _audit(cur, settings, days),
        "cost": _cost(cur, days),
    }


def _ratio(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


def _seconds(value) -> float | None:
    return float(value) if value is not None else None


def _retrieval_quality(cur, days: int) -> dict[str, Any]:
    # Useful-read ratio: of the distinct memories delivered by READ
    # events in the window, the fraction reinforced (or confirmed) at or
    # after their first delivery — recall that earned its context tokens.
    reads = cur.execute(
        "WITH reads AS ("
        "  SELECT (jsonb_array_elements_text(details->'memory_ids'))::uuid AS memory_id,"
        "         min(at) AS first_read"
        "  FROM memory_events"
        f"  WHERE action='READ' AND at >= {WINDOW}"
        "  GROUP BY 1)"
        " SELECT count(*) AS delivered,"
        "        count(*) FILTER (WHERE EXISTS ("
        "          SELECT 1 FROM memory_events e"
        "          WHERE e.memory_id = reads.memory_id"
        "            AND e.action IN ('REINFORCE','CONFIRM')"
        "            AND e.at >= reads.first_read)) AS reinforced"
        " FROM reads",
        (days,),
    ).fetchone()

    # Staged-tier precision: how staged memories left the tier in the
    # window — promotion (the pipeline was right) vs review rejection or
    # archival (it was noise). The health of extraction (doc 07 §4).
    staged = cur.execute(
        "SELECT"
        "  count(*) FILTER (WHERE action='PROMOTE_STATUS'"
        "                     AND details->>'from'='staged') AS promoted,"
        "  count(*) FILTER (WHERE action='REJECT' AND memory_id IS NOT NULL) AS rejected,"
        "  count(*) FILTER (WHERE action='ARCHIVE'"
        "                     AND details->>'from'='staged') AS archived"
        f" FROM memory_events WHERE at >= {WINDOW}",
        (days,),
    ).fetchone()
    resolved = staged["promoted"] + staged["rejected"] + staged["archived"]

    queue = cur.execute(
        "SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open,"
        "       count(*) FILTER (WHERE resolved_at IS NULL"
        "                          AND escalated_at IS NOT NULL) AS escalated,"
        "       EXTRACT(epoch FROM now() - min(queued_at)"
        "                                  FILTER (WHERE resolved_at IS NULL)) AS oldest_s"
        " FROM contradiction_queue",
    ).fetchone()

    return {
        "memories_read": reads["delivered"],
        "memories_reinforced_after_read": reads["reinforced"],
        "useful_read_ratio": _ratio(reads["reinforced"], reads["delivered"]),
        "staged_promoted": staged["promoted"],
        "staged_rejected": staged["rejected"],
        "staged_archived": staged["archived"],
        "staged_precision": _ratio(staged["promoted"], resolved),
        "contradiction_queue": {
            "open": queue["open"],
            "escalated": queue["escalated"],
            "oldest_age_seconds": _seconds(queue["oldest_s"]),
        },
    }


def _policy(cur, days: int) -> dict[str, Any]:
    # Decisions by verdict per agent: a `deny` spike is a misconfigured
    # agent or an attack; an `ask` spike is user-fatigue risk (doc 07 §4).
    by_verdict: dict[str, dict[str, int]] = {}
    for row in cur.execute(
        "SELECT actor, details->>'verdict' AS verdict, count(*) AS n"
        " FROM memory_events"
        f" WHERE action='POLICY_DECISION' AND at >= {WINDOW}"
        " GROUP BY 1, 2 ORDER BY 1, 2",
        (days,),
    ).fetchall():
        by_verdict.setdefault(row["actor"], {})[row["verdict"]] = row["n"]

    asks = cur.execute(
        "SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open,"
        "       EXTRACT(epoch FROM now() - min(created_at)"
        "                                  FILTER (WHERE resolved_at IS NULL)) AS oldest_s,"
        f"      count(*) FILTER (WHERE resolved_at >= {WINDOW}) AS resolved,"
        "       percentile_cont(0.5) WITHIN GROUP"
        "         (ORDER BY EXTRACT(epoch FROM resolved_at - created_at))"
        f"        FILTER (WHERE resolved_at >= {WINDOW}) AS median_s"
        " FROM pending_actions",
        (days, days),
    ).fetchone()

    return {
        "decisions_by_verdict": by_verdict,
        "asks": {
            "open": asks["open"],
            "oldest_open_seconds": _seconds(asks["oldest_s"]),
            "resolved": asks["resolved"],
            "median_time_to_confirm_seconds": _seconds(asks["median_s"]),
        },
    }


def _lifecycle(cur, days: int) -> dict[str, Any]:
    population: dict[str, dict[str, int]] = {}
    for row in cur.execute(
        "SELECT s.family, m.status, count(*) AS n"
        " FROM memories m JOIN scopes s ON s.id = m.scope_id"
        " GROUP BY 1, 2 ORDER BY 1, 2",
    ).fetchall():
        population.setdefault(row["family"], {})[row["status"]] = row["n"]

    # The decay sweep archives with reason 'decayed …' (doc 03 §5).
    decay = cur.execute(
        "SELECT count(*) AS n FROM memory_events"
        f" WHERE action='ARCHIVE' AND at >= {WINDOW}"
        "   AND details->>'reason' LIKE 'decayed%%'",
        (days,),
    ).fetchone()

    staged_to_active = cur.execute(
        "SELECT percentile_cont(0.5) WITHIN GROUP"
        "         (ORDER BY EXTRACT(epoch FROM e.at - m.recorded_at)) AS median_s"
        " FROM memory_events e JOIN memories m ON m.id = e.memory_id"
        " WHERE e.action='PROMOTE_STATUS' AND e.details->>'from'='staged'"
        f"  AND e.at >= {WINDOW}",
        (days,),
    ).fetchone()

    return {
        "tier_population": population,
        "decay_archived": decay["n"],
        "median_staged_to_active_seconds": _seconds(staged_to_active["median_s"]),
    }


def _audit(cur, settings: Settings, days: int) -> dict[str, Any]:
    log = cur.execute(
        f"SELECT count(*) AS n, max(at) AS last_at FROM memory_events WHERE at >= {WINDOW}",
        (days,),
    ).fetchone()

    requests = cur.execute(
        "SELECT count(*) FILTER (WHERE completed_at IS NULL) AS open,"
        "       EXTRACT(epoch FROM now() - min(created_at)"
        "                                  FILTER (WHERE completed_at IS NULL)) AS oldest_s,"
        f"      count(*) FILTER (WHERE completed_at >= {WINDOW}) AS completed,"
        "       percentile_cont(0.5) WITHIN GROUP"
        "         (ORDER BY EXTRACT(epoch FROM completed_at - created_at))"
        f"        FILTER (WHERE completed_at >= {WINDOW}) AS median_s,"
        "       max(EXTRACT(epoch FROM completed_at - created_at))"
        f"        FILTER (WHERE completed_at >= {WINDOW}) AS max_s,"
        f"      count(*) FILTER (WHERE completed_at >= {WINDOW}"
        "          AND completed_at - created_at"
        "              > make_interval(secs => %s)) AS over_slo"
        " FROM erasure_requests",
        (days, days, days, days, ERASURE_SLO_SECONDS),
    ).fetchone()

    # Attestation coverage: completed requests whose signature still
    # verifies against the current key — presence alone is not coverage.
    attested = 0
    for row in cur.execute(
        "SELECT attestation FROM erasure_requests"
        f" WHERE completed_at >= {WINDOW} AND attestation IS NOT NULL",
        (days,),
    ).fetchall():
        if erasure.verify_attestation(row["attestation"], settings.attestation_key):
            attested += 1

    return {
        "event_log": {
            "events": log["n"],
            "last_event_at": log["last_at"].isoformat() if log["last_at"] else None,
            # Events commit in the write's transaction (ADR-0004): zero by
            # construction in this stack, reported for shape stability.
            "lag_seconds": 0.0,
        },
        "erasure": {
            "open": requests["open"],
            "oldest_open_seconds": _seconds(requests["oldest_s"]),
            "completed": requests["completed"],
            "median_completion_seconds": _seconds(requests["median_s"]),
            "max_completion_seconds": _seconds(requests["max_s"]),
            "slo_seconds": ERASURE_SLO_SECONDS,
            "over_slo": requests["over_slo"],
            "attestation_coverage": _ratio(attested, requests["completed"]),
        },
    }


def _cost(cur, days: int) -> dict[str, Any]:
    # Consolidator attention per scope. The reference seams are
    # deterministic (no LLM spend), so the countable unit is evented
    # actions; a real judge/reflector seam adds tokens to the same shape.
    by_scope: dict[str, int] = {}
    for row in cur.execute(
        "SELECT coalesce(scope_id, '-') AS scope_id, count(*) AS n"
        " FROM memory_events"
        f" WHERE actor='system:consolidator' AND at >= {WINDOW}"
        " GROUP BY 1 ORDER BY n DESC, 1 LIMIT 20",
        (days,),
    ).fetchall():
        by_scope[row["scope_id"]] = row["n"]

    embeddings = cur.execute(
        "SELECT count(*) AS n FROM memories"
        f" WHERE content_embedding IS NOT NULL AND recorded_at >= {WINDOW}",
        (days,),
    ).fetchone()

    # ADR-0006's write amplification, with its own mitigation visible:
    # one batched READ event per delivery, N memory ids inside.
    reads = cur.execute(
        "SELECT count(*) AS events,"
        "       coalesce(sum(jsonb_array_length(details->'memory_ids')), 0) AS delivered"
        f" FROM memory_events WHERE action='READ' AND at >= {WINDOW}",
        (days,),
    ).fetchone()

    return {
        "consolidator_events_by_scope": by_scope,
        "embeddings_written": embeddings["n"],
        "reads": {
            "read_events": reads["events"],
            "memories_delivered": reads["delivered"],
            "batching_ratio": _ratio(reads["delivered"], reads["events"]),
        },
    }
