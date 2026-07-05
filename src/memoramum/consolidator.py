"""The consolidator (doc 07 §2) — the full job set (P4).

The system's sleep-time worker: memory quality is produced offline so the
hot path stays fast and dumb. P2 brought **status promotion**,
**contradiction review** (escalate / archive timers; the weighing itself
stays human via the review surface without a model judge), and the
**decay & TTL sweeps** of doc 03 §5; P3 taught the TTL sweep the
per-category retention overrides (tombstone at expiry where policy
demands hard deletion). P4 completes the doc 07 §2 table:
**dedupe/merge** (judge-confirmed duplicates within a scope merge into a
consolidated successor; predecessors deprecated — Mem0's ADD/UPDATE/NOOP
decision, moved off the write path), **reflection/summarization**
(clusters of related episodic memories distilled into candidates that go
through the write pipeline like any agent's; procedural outputs default
to `ask`), and **hygiene** (reclassification after classifier upgrades,
the doc 06 §3 poisoning anomaly checks, and orphan/ghost-vector
verification after erasures).

Reflection is LLM-shaped work; like the judge it hides behind a
one-method seam (`Reflector`) whose default is 'none' — off without a
model, with a deterministic 'theme' stand-in for dev and tests.

It acts as `system:consolidator`: every action is evented like any
principal. All jobs are idempotent — a wedged consolidator degrades
quality, never correctness. Run once with `memoramum-consolidate`
(`make consolidate`); schedule it nightly in production.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from . import lifecycle, policy, retrieval
from .principals import Flow, Principal
from .service import MemoryService

CONSOLIDATOR = Principal("system:consolidator")

# Staged content that reads like an instruction to the agent is the
# poisoning shape worth flagging (doc 06 §3: "unusually directive").
_DIRECTIVE = re.compile(
    r"(?i)\b(always|never|ignore|disregard|instead you must|you must|from now on)\b"
)


# ---------- the reflection seam (doc 07 §2) ----------

@dataclass(frozen=True)
class ReflectionCandidate:
    content: str
    kind: str                              # 'semantic' | 'procedural'
    source_memory_ids: tuple[str, ...]
    categories: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    justification: str = ""


class Reflector(Protocol):
    version: str

    def reflect(self, memories: list[dict]) -> list[ReflectionCandidate]: ...


class NoneReflector:
    """Reflection off (the default): distilling clusters into new
    statements is LLM-shaped work, like the contradiction judge."""

    version = "none-0"

    def reflect(self, memories: list[dict]) -> list[ReflectionCandidate]:
        return []


class ThemeReflector:
    """Deterministic dev/test stand-in: episodic memories sharing the same
    categories and subjects form a theme; a big-enough theme distills into
    one semantic candidate restating its strongest member. No semantics —
    a model reflector slots in behind the same seam; do not deploy."""

    version = "theme-1"

    def __init__(self, min_cluster: int):
        self.min_cluster = min_cluster

    def reflect(self, memories: list[dict]) -> list[ReflectionCandidate]:
        themes: dict[tuple, list[dict]] = {}
        for m in memories:
            key = (tuple(sorted(m["categories"])), tuple(sorted(m["subject_ids"])))
            themes.setdefault(key, []).append(m)
        out = []
        for (categories, subjects), cluster in sorted(themes.items()):
            if len(cluster) < self.min_cluster:
                continue
            strongest = max(cluster, key=lambda m: (m["strength"], m["recorded_at"], str(m["id"])))
            out.append(ReflectionCandidate(
                content=f"Recurring across related episodes: {strongest['content']}",
                kind="semantic",
                source_memory_ids=tuple(sorted(str(m["id"]) for m in cluster)),
                categories=categories, subjects=subjects,
                justification=f"distilled from {len(cluster)} related episodic memories"
                              " (Generative-Agents reflection, doc 07 §2)",
            ))
        return out


def make_reflector(name: str, min_cluster: int) -> Reflector:
    if name == "none":
        return NoneReflector()
    if name == "theme":
        return ThemeReflector(min_cluster)
    raise ValueError(f"unknown reflector {name!r} (expected 'none' or 'theme')")


# A join fragment: memories under an unresolved quarantine are frozen out
# of every consolidation job, not just retrieval (doc 06 §3).
_NOT_QUARANTINED = (
    "NOT EXISTS (SELECT 1 FROM quarantine_items qi"
    " WHERE qi.memory_id = m.id AND qi.resolved_at IS NULL)"
)


class Consolidator:
    def __init__(self, service: MemoryService):
        self.svc = service
        self.settings = service.settings
        self.reflector = make_reflector(
            self.settings.reflector, self.settings.reflection_min_cluster
        )

    def run(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        return {  # the doc 07 §2 job order
            "merged": self.dedupe(now),
            "contradictions": self.review_contradictions(now),
            "promoted": self.promote_staged(now),
            "reflected": self.reflect(now),
            "swept": self.sweep(now),
            "hygiene": self.hygiene(now),
        }

    # ---------- dedupe / merge (doc 07 §2) ----------

    def dedupe(self, now: datetime) -> list[dict]:
        """Judge-confirmed duplicates within a scope merge into one
        consolidated successor (weakest tier, minimum trust, summed
        reinforcement); predecessors are deprecated with the successor
        linked. The write path already reinforces same-chain duplicates
        (doc 03 §4) — what lands here are copies that arrived through
        different chains, e.g. a promotion into a scope that already knew
        the fact. Candidate generation is a per-scope pairwise pass in the
        reference implementation (embedding-neighborhood pre-filtering is
        an optimization, not a semantic)."""
        merged: list[dict] = []
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()
            scope_ids = [r["scope_id"] for r in cur.execute(
                "SELECT m.scope_id FROM memories m"
                f" WHERE m.status IN ('staged','active') AND {_NOT_QUARANTINED}"
                " GROUP BY m.scope_id HAVING count(*) > 1 ORDER BY m.scope_id",
            ).fetchall()]
            for scope_id in scope_ids:
                rows = cur.execute(
                    "SELECT m.* FROM memories m WHERE m.scope_id=%s"
                    f" AND m.status IN ('staged','active') AND {_NOT_QUARANTINED}"
                    " ORDER BY m.recorded_at",
                    (scope_id,),
                ).fetchall()
                taken: set[str] = set()
                for i, a in enumerate(rows):
                    if str(a["id"]) in taken:
                        continue
                    group = [a]
                    for b in rows[i + 1:]:
                        if str(b["id"]) in taken:
                            continue
                        if self.svc.judge.judge(b["content"], a["content"]) == "duplicate":
                            group.append(b)
                            taken.add(str(b["id"]))
                    if len(group) < 2:
                        continue
                    winner = max(group, key=lambda m: (
                        m["status"] == "active", m["trust_score"], -m["recorded_at"].timestamp()
                    ))
                    successor = self.svc._consolidated_insert(
                        cur, CONSOLIDATOR, group, scope_id=scope_id,
                        content=winner["content"], kind=winner["kind"], job="dedupe",
                        justification=f"merged {len(group)} duplicates in {scope_id}"
                                      " (doc 07 §2 dedupe/merge)",
                    )
                    for m in group:
                        cur.execute(
                            "UPDATE memories SET status='deprecated', superseded_by=%s"
                            " WHERE id=%s",
                            (successor, m["id"]),
                        )
                        self.svc._event(
                            cur, CONSOLIDATOR, "DEPRECATE", memory_id=str(m["id"]),
                            scope_id=scope_id, details={"merged_into": successor},
                        )
                    merged.append({"scope_id": scope_id, "successor": successor,
                                   "merged": [str(m["id"]) for m in group]})
        return merged

    # ---------- reflection / summarization (doc 07 §2) ----------

    def reflect(self, now: datetime) -> list[dict]:
        """Clusters of related episodic memories → distilled candidates.
        Candidates go through the write pipeline like any agent's: policy
        applies, `consolidated` procedural candidates default to `ask`
        (the builtin org layer), and re-runs deduplicate against the
        previous night's output."""
        proposals: list[dict] = []
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()
            scope_rows = cur.execute(
                "SELECT m.scope_id, s.surface FROM memories m JOIN scopes s ON s.id = m.scope_id"
                " WHERE m.kind='episodic' AND m.status IN ('staged','active')"
                f" AND {_NOT_QUARANTINED}"
                " GROUP BY m.scope_id, s.surface HAVING count(*) >= %s ORDER BY m.scope_id",
                (self.settings.reflection_min_cluster,),
            ).fetchall()
            clusters = []
            for sr in scope_rows:
                rows = cur.execute(
                    "SELECT m.* FROM memories m WHERE m.scope_id=%s AND m.kind='episodic'"
                    f" AND m.status IN ('staged','active') AND {_NOT_QUARANTINED}"
                    " ORDER BY m.recorded_at",
                    (sr["scope_id"],),
                ).fetchall()
                clusters.append((sr, self.reflector.reflect(rows)))
        for sr, candidates in clusters:
            flow = Flow(surface=sr["surface"], container=sr["scope_id"],
                        session_id=f"reflection:{sr['scope_id']}")
            for cand in candidates:
                verdict = self.svc.remember(
                    CONSOLIDATOR, flow, content=cand.content, kind=cand.kind,
                    origin_kind="consolidated", subjects=list(cand.subjects),
                    categories=list(cand.categories), justification=cand.justification,
                    source_memory_ids=list(cand.source_memory_ids),
                )
                proposals.append({"scope_id": sr["scope_id"], "content": cand.content,
                                  "decision": verdict["decision"],
                                  "memory_id": verdict.get("memory_id")})
        return proposals

    # ---------- status promotion (doc 03 §4) ----------

    def promote_staged(self, now: datetime) -> list[str]:
        """Apply the reinforcement rules; staged→active transitions batch
        here (plus the write-path micro-runs)."""
        promoted: list[str] = []
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()
            for row in cur.execute("SELECT * FROM memories WHERE status='staged'").fetchall():
                if lifecycle.promotion_due(cur, row, self.settings, now):
                    self.svc._promote(cur, CONSOLIDATOR, row)
                    promoted.append(str(row["id"]))
        return promoted

    # ---------- contradiction review (doc 03 §3, §5) ----------

    def review_contradictions(self, now: datetime) -> dict:
        """Work the held-contradiction queue timers: escalate to human
        review after 7 days, archive the challenger after 30 (doc 03 §5)."""
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()
            escalated = [
                str(r["id"]) for r in cur.execute(
                    "UPDATE contradiction_queue SET escalated_at=%s"
                    " WHERE resolved_at IS NULL AND escalated_at IS NULL AND queued_at <= %s"
                    " RETURNING id",
                    (now, now - timedelta(days=self.settings.contradiction_escalate_days)),
                ).fetchall()
            ]
            archived: list[str] = []
            stale = cur.execute(
                "SELECT q.id AS queue_id, m.* FROM contradiction_queue q"
                " JOIN memories m ON m.id = q.challenger_id"
                " WHERE q.resolved_at IS NULL AND q.queued_at <= %s",
                (now - timedelta(days=self.settings.contradiction_archive_days),),
            ).fetchall()
            for row in stale:
                if row["status"] == "staged":
                    self.svc._archive(cur, CONSOLIDATOR, row, "held contradiction unresolved")
                cur.execute(
                    "UPDATE contradiction_queue SET resolved_at=%s, resolution='archived'"
                    " WHERE id=%s",
                    (now, row["queue_id"]),
                )
                archived.append(str(row["id"]))
        return {"escalated": escalated, "archived": archived}

    # ---------- decay & sweeps (doc 03 §5) ----------

    def sweep(self, now: datetime) -> dict:
        out: dict = {}
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()

            # Staged, never retrieved, no reinforcement → archive after 30 days.
            idle = cur.execute(
                "SELECT m.* FROM memories m WHERE m.status='staged' AND m.access_count=0"
                " AND m.recorded_at <= %s AND NOT EXISTS ("
                "   SELECT 1 FROM memory_events e WHERE e.memory_id=m.id"
                "   AND e.action IN ('REINFORCE','CONFIRM'))",
                (now - timedelta(days=self.settings.staged_idle_archive_days),),
            ).fetchall()
            for row in idle:
                self.svc._archive(cur, CONSOLIDATOR, row, "staged and never used")
            out["staged_idle"] = [str(r["id"]) for r in idle]

            # Active, R below threshold → PROPOSE-style event first (so the
            # candidacy is visible), archive on a later run past the grace.
            flagged, archived = [], []
            for row in cur.execute("SELECT * FROM memories WHERE status='active'").fetchall():
                if retrieval.retention(row, self.settings, now) >= self.settings.decay_archive_threshold:
                    continue
                proposal = cur.execute(
                    "SELECT at FROM memory_events WHERE memory_id=%s AND action='PROPOSE'"
                    " AND details->>'proposal'='archive' ORDER BY at DESC LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if proposal is None:
                    self.svc._event(
                        cur, CONSOLIDATOR, "PROPOSE", memory_id=str(row["id"]),
                        scope_id=row["scope_id"],
                        details={"proposal": "archive", "reason": "decayed",
                                 "retention": retrieval.retention(row, self.settings, now)},
                    )
                    flagged.append(str(row["id"]))
                elif now - proposal["at"] >= timedelta(days=self.settings.decay_archive_grace_days):
                    self.svc._archive(cur, CONSOLIDATOR, row, "decayed (R below threshold)")
                    archived.append(str(row["id"]))
            out["decay_flagged"], out["decay_archived"] = flagged, archived

            # Policy TTL reached → archive, or tombstone where a retention
            # override demands hard deletion at expiry (doc 03 §5, e.g.
            # anything tagged `health`).
            expired = cur.execute(
                "SELECT m.*, p.responsible_agent, s.surface FROM memories m"
                " LEFT JOIN memory_provenance p ON p.memory_id = m.id"
                " LEFT JOIN scopes s ON s.id = m.scope_id"
                " WHERE m.expires_at IS NOT NULL AND m.expires_at <= %s"
                " AND m.status IN ('staged','active','invariant')",
                (now,),
            ).fetchall()
            out["ttl"], out["ttl_tombstoned"] = [], []
            for row in expired:
                layers = self.svc._layers(
                    cur, agent=row["responsible_agent"] or "system:consolidator",
                    surface=row["surface"], subjects=row["subject_ids"],
                )
                override = policy.retention_override(layers, list(row["categories"]))
                if override and override["expiry_mode"] == "tombstone":
                    self.svc._tombstone(cur, CONSOLIDATOR, row, "expires_at reached")
                    out["ttl_tombstoned"].append(str(row["id"]))
                else:
                    self.svc._archive(cur, CONSOLIDATOR, row, "expires_at reached")
                    out["ttl"].append(str(row["id"]))

            # Deprecated older than the history-retention window → archive.
            stale = cur.execute(
                "SELECT m.* FROM memories m WHERE m.status='deprecated' AND EXISTS ("
                "   SELECT 1 FROM memory_events e WHERE e.memory_id=m.id"
                "   AND e.action IN ('SUPERSEDE','DEPRECATE')"
                "   GROUP BY e.memory_id HAVING max(e.at) <= %s)",
                (now - timedelta(days=self.settings.history_retention_days),),
            ).fetchall()
            for row in stale:
                self.svc._archive(cur, CONSOLIDATOR, row, "history retention window passed")
            out["history_retention"] = [str(r["id"]) for r in stale]
        return out

    # ---------- hygiene (doc 07 §2, doc 06 §3–4) ----------

    def hygiene(self, now: datetime) -> dict:
        out: dict = {"reclassified": [], "quarantined": [], "flagged": [], "ghosts_repaired": []}
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()

            # Reclassification after a classifier upgrade (doc 06 §4):
            # classifier versions live in provenance activity; a stale
            # version gets a rescan. Content a newer classifier would have
            # blocked is quarantined for review, never silently kept.
            stale = cur.execute(
                "SELECT m.*, p.activity FROM memories m"
                " JOIN memory_provenance p ON p.memory_id = m.id"
                " WHERE m.status IN ('staged','active','invariant')"
                " AND p.activity ? 'classifier'"
                " AND p.activity->>'classifier' IS DISTINCT FROM %s",
                (self.svc.analyzer.version,),
            ).fetchall()
            quarantine_req = None
            for row in stale:
                entities = self.svc.analyzer.analyze(row["content"])
                blockers = [e.kind for e in entities if e.kind in ("credential", "gov_id")]
                if blockers:
                    if quarantine_req is None:
                        quarantine_req = str(cur.execute(
                            "INSERT INTO quarantine_requests (predicate, requested_by, note)"
                            " VALUES (%s,%s,%s) RETURNING id",
                            (json.dumps({"reclassification": self.svc.analyzer.version}),
                             CONSOLIDATOR.actor, "reclassification sweep found blockers"),
                        ).fetchone()["id"])
                    cur.execute(
                        "INSERT INTO quarantine_items (request_id, memory_id)"
                        " VALUES (%s,%s) ON CONFLICT DO NOTHING",
                        (quarantine_req, row["id"]),
                    )
                    self.svc._event(
                        cur, CONSOLIDATOR, "QUARANTINE", memory_id=str(row["id"]),
                        scope_id=row["scope_id"],
                        details={"request_id": quarantine_req,
                                 "reason": f"reclassification found {sorted(set(blockers))}"},
                    )
                    out["quarantined"].append(str(row["id"]))
                cur.execute(
                    "UPDATE memory_provenance SET activity = activity || %s WHERE memory_id=%s",
                    (json.dumps({"classifier": self.svc.analyzer.version,
                                 "reclassified_from": row["activity"].get("classifier")}),
                     row["id"]),
                )
                out["reclassified"].append(str(row["id"]))

            # Poisoning anomaly checks (doc 06 §3 detection): each flag is
            # a PROPOSE(review) event — visible, once, into the review flow.
            def flag(row, reason: str) -> None:
                already = cur.execute(
                    "SELECT 1 FROM memory_events WHERE memory_id=%s AND action='PROPOSE'"
                    " AND details->>'proposal'='review' AND details->>'reason'=%s LIMIT 1",
                    (row["id"], reason),
                ).fetchone()
                if already:
                    return
                self.svc._event(
                    cur, CONSOLIDATOR, "PROPOSE", memory_id=str(row["id"]),
                    scope_id=row["scope_id"],
                    details={"proposal": "review", "reason": reason},
                )
                out["flagged"].append({"memory_id": str(row["id"]), "reason": reason})

            for row in cur.execute(
                "SELECT m.* FROM memories m WHERE m.status='staged'",
            ).fetchall():
                if _DIRECTIVE.search(row["content"]):
                    flag(row, "directive_content")

            spikes = cur.execute(
                "SELECT e.author, array_agg(DISTINCT m.id) AS ids"
                " FROM memories m"
                " JOIN memory_derivations d ON d.memory_id = m.id AND d.source_type='episode'"
                " JOIN episodes e ON e.id = d.source_id"
                " WHERE m.status='staged' AND m.recorded_at >= %s AND e.author IS NOT NULL"
                " GROUP BY e.author HAVING count(DISTINCT m.id) > %s",
                (now - timedelta(days=1), self.settings.anomaly_author_daily_writes),
            ).fetchall()
            for spike in spikes:
                for mid in spike["ids"]:
                    row = cur.execute("SELECT * FROM memories WHERE id=%s", (mid,)).fetchone()
                    flag(row, "write_rate_spike")

            outliers = cur.execute(
                "WITH pop AS ("
                "  SELECT m.scope_id, avg(m.content_embedding) AS centroid, count(*) AS n"
                "  FROM memories m WHERE m.status IN ('staged','active')"
                "  AND m.content_embedding IS NOT NULL GROUP BY m.scope_id"
                ") SELECT m.* FROM memories m JOIN pop ON pop.scope_id = m.scope_id"
                " WHERE pop.n >= %s AND m.status='staged' AND m.content_embedding IS NOT NULL"
                " AND (m.content_embedding <=> pop.centroid) > %s",
                (self.settings.outlier_min_scope_size, self.settings.outlier_distance),
            ).fetchall()
            for row in outliers:
                flag(row, "embedding_outlier")

            # Orphan/ghost-vector verification after erasures (doc 06 §2):
            # a tombstone with content or a vector left behind is a bug —
            # repair it and leave the repair on the record.
            ghosts = cur.execute(
                "UPDATE memories SET content='', content_embedding=NULL"
                " WHERE status='tombstoned' AND (content <> '' OR content_embedding IS NOT NULL)"
                " RETURNING id, scope_id",
            ).fetchall()
            for g in ghosts:
                self.svc._event(
                    cur, CONSOLIDATOR, "TOMBSTONE", memory_id=str(g["id"]),
                    scope_id=g["scope_id"],
                    details={"repair": True, "reason": "ghost content/vector found by hygiene"},
                )
                out["ghosts_repaired"].append(str(g["id"]))
        return out


def main() -> None:
    from .config import settings_from_env
    from .db import make_pool

    settings = settings_from_env()
    service = MemoryService(make_pool(settings.database_url), settings)
    summary = Consolidator(service).run()
    print(summary)
