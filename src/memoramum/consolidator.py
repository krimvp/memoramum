"""The consolidator (doc 07 §2) — the P2–P3 job subset.

The system's sleep-time worker: memory quality is produced offline so the
hot path stays fast and dumb. P2 brought **status promotion**,
**contradiction review** (escalate / archive timers; the weighing itself
is human via the review surface until the P4 LLM jobs land), and the
**decay & TTL sweeps** of doc 03 §5; P3 teaches the TTL sweep the
per-category retention overrides (tombstone at expiry where policy
demands hard deletion). Dedupe/merge and reflection/summarization arrive
in P4 with the full job set.

It acts as `system:consolidator`: every action is evented like any
principal. All jobs are idempotent — a wedged consolidator degrades
quality, never correctness. Run once with `memoramum-consolidate`
(`make consolidate`); schedule it nightly in production.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import lifecycle, policy, retrieval
from .principals import Principal
from .service import MemoryService

CONSOLIDATOR = Principal("system:consolidator")


class Consolidator:
    def __init__(self, service: MemoryService):
        self.svc = service
        self.settings = service.settings

    def run(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        return {
            "promoted": self.promote_staged(now),
            "contradictions": self.review_contradictions(now),
            "swept": self.sweep(now),
        }

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


def main() -> None:
    from .config import settings_from_env
    from .db import make_pool

    settings = settings_from_env()
    service = MemoryService(make_pool(settings.database_url), settings)
    summary = Consolidator(service).run()
    print(summary)
