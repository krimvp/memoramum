"""Extraction workers (doc 07 §1): `agent_observed` learning.

Debounced background reflection over recent episodes per scope — the
LangMem ReflectionExecutor pattern: accumulate, run at conversation-lull
(default debounce 30 min), never wait past the cap (4 h). The batch
worker here is the polling equivalent of cancel-and-reschedule: a scope
is due when its newest unprocessed episode is older than the debounce,
or its oldest has waited out the cap.

Extraction proposes candidates **through the same write pipeline as any
agent** — policy applies identically (staged by default, PII pipeline,
enrollment gate, POLICY_DECISION events). The worker acts as the agent
enrolled to write in the scope (the scenario's Sage observing #deploys),
with `origin_kind=agent_observed`; a scope no agent may write to learns
nothing, exactly as default-deny demands.

Turning episodes into candidate memories is LLM-shaped work; like the
embedder, judge and PII analyzer it hides behind a one-method seam with
a deterministic local implementation. Run once with `memoramum-extract`
(`make extract`); schedule it in production.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol

from .principals import Flow, Principal


@dataclass(frozen=True)
class ExtractionCandidate:
    content: str
    kind: str = "semantic"
    categories: tuple[str, ...] = ()
    subjects: tuple[str, ...] = ()
    justification: str = ""
    source_episode_ids: tuple[str, ...] = ()


class Extractor(Protocol):
    version: str

    def extract(self, episodes: list[dict]) -> list[ExtractionCandidate]: ...


class NoneExtractor:
    """Extraction off; episodes accumulate for a model extractor."""

    version = "none-0"

    def extract(self, episodes: list[dict]) -> list[ExtractionCandidate]:
        return []


class MarkerExtractor:
    """Deterministic dev/test stand-in for a model extractor: statements
    their author flagged as standing knowledge ("reminder:", "note:",
    "PSA:", …) become semantic candidates. No inference — a real model
    extractor slots in behind the same seam; do not deploy."""

    version = "marker-1"

    _MARKER = re.compile(
        r"(?im)^\s*(?:reminder|note|remember|heads[- ]?up|psa|fyi)[:,]\s*(?P<fact>.+?)\s*$"
    )
    _CATEGORY_HINTS: tuple[tuple[str, re.Pattern], ...] = (
        ("process", re.compile(r"(?i)\b(deploy|release|pipeline|schedule|process|review)s?\b")),
        ("preference", re.compile(r"(?i)\bprefers?\b")),
    )

    def extract(self, episodes: list[dict]) -> list[ExtractionCandidate]:
        out: list[ExtractionCandidate] = []
        for ep in episodes:
            for m in self._MARKER.finditer(ep["content"] or ""):
                fact = m.group("fact")
                categories = tuple(
                    c for c, pattern in self._CATEGORY_HINTS if pattern.search(fact)
                )
                out.append(
                    ExtractionCandidate(
                        content=fact,
                        categories=categories,
                        justification=(
                            f"standing statement flagged by {ep['author'] or 'an unknown author'};"
                            " proposed by background extraction (doc 07 §1)"
                        ),
                        source_episode_ids=(str(ep["id"]),),
                    )
                )
        return out


def make_extractor(name: str) -> Extractor:
    if name == "none":
        return NoneExtractor()
    if name == "marker":
        return MarkerExtractor()
    raise ValueError(f"unknown extractor {name!r} (expected 'none' or 'marker')")


class ExtractionWorker:
    def __init__(self, service):
        self.svc = service
        self.settings = service.settings
        self.extractor = make_extractor(self.settings.extractor)

    def run(self, now: datetime | None = None, *, only_scopes: list[str] | None = None) -> dict:
        """Process every due scope (a worker deployment may shard with
        `only_scopes`)."""
        now = now or datetime.now(timezone.utc)
        out: dict = {}
        for scope in self.due_scopes(now):
            if only_scopes is not None and scope["scope_id"] not in only_scopes:
                continue
            out[scope["scope_id"]] = self._run_scope(scope["scope_id"])
        return out

    def due_scopes(self, now: datetime) -> list[dict]:
        """Scopes with unprocessed episode content, at conversation-lull
        (newest older than the debounce) or past the accumulation cap."""
        debounce = timedelta(minutes=self.settings.extraction_debounce_minutes)
        cap = timedelta(hours=self.settings.extraction_cap_hours)
        with self.svc.pool.connection() as conn:
            rows = conn.cursor().execute(
                "SELECT e.scope_id, min(e.occurred_at) AS oldest, max(e.occurred_at) AS newest"
                " FROM episodes e LEFT JOIN extraction_state x ON x.scope_id = e.scope_id"
                " WHERE e.content IS NOT NULL"
                " AND e.recorded_at > coalesce(x.processed_through, '-infinity')"
                " GROUP BY e.scope_id ORDER BY e.scope_id",
            ).fetchall()
        return [r for r in rows if r["newest"] <= now - debounce or r["oldest"] <= now - cap]

    def _run_scope(self, scope_id: str) -> dict:
        with self.svc.pool.connection() as conn:
            cur = conn.cursor()
            episodes = cur.execute(
                "SELECT e.* FROM episodes e LEFT JOIN extraction_state x ON x.scope_id = e.scope_id"
                " WHERE e.scope_id = %s AND e.content IS NOT NULL"
                " AND e.recorded_at > coalesce(x.processed_through, '-infinity')"
                " ORDER BY e.occurred_at",
                (scope_id,),
            ).fetchall()
            scope = cur.execute("SELECT * FROM scopes WHERE id=%s", (scope_id,)).fetchone()
            agent = self._responsible_agent(cur, scope_id)

        result: dict = {"episodes": len(episodes), "proposed": []}
        if agent is None:
            # No agent is enrolled to write here: nothing may be learned
            # (default deny); the watermark still advances — these episodes
            # are not owed a retry.
            result["skipped"] = "no writer_agent enrolled in the scope chain"
        else:
            participants = tuple(
                sorted({e["author"] for e in episodes if e["author"] and e["author"].startswith("user:")})
            )
            flow = Flow(surface=scope["surface"], container=scope_id,
                        participants=participants, session_id=f"extraction:{scope_id}")
            for cand in self.extractor.extract(episodes):
                verdict = self.svc.remember(
                    Principal(agent), flow,
                    content=cand.content, kind=cand.kind, origin_kind="agent_observed",
                    subjects=list(cand.subjects), categories=list(cand.categories),
                    justification=cand.justification,
                    source_episode_ids=list(cand.source_episode_ids),
                )
                result["proposed"].append(
                    {"content": cand.content, "decision": verdict["decision"],
                     "memory_id": verdict.get("memory_id")}
                )
        with self.svc.pool.connection() as conn:
            conn.cursor().execute(
                "INSERT INTO extraction_state (scope_id, processed_through, last_run_at)"
                " VALUES (%s, %s, now())"
                " ON CONFLICT (scope_id) DO UPDATE"
                " SET processed_through = excluded.processed_through, last_run_at = now()",
                (scope_id, max(e["recorded_at"] for e in episodes)),
            )
        return result

    def _responsible_agent(self, cur, scope_id: str) -> str | None:
        """The agent the extraction acts as: the nearest writer_agent
        enrollment walking up the scope chain (deterministic: first by
        name on ties)."""
        row = cur.execute(
            """
            WITH RECURSIVE up AS (
                SELECT s.*, 0 AS depth FROM scopes s WHERE s.id = %s
                UNION ALL
                SELECT p.*, up.depth + 1 FROM scopes p JOIN up ON p.id = up.parent_scope_id
            )
            SELECT r.principal FROM scope_relations r JOIN up ON up.id = r.scope_id
            WHERE r.relation = 'writer_agent' AND r.principal LIKE 'agent:%%'
            ORDER BY up.depth, r.principal LIMIT 1
            """,
            (scope_id,),
        ).fetchone()
        return row["principal"] if row else None


def main() -> None:
    from .config import settings_from_env
    from .db import make_pool
    from .service import MemoryService

    settings = settings_from_env()
    service = MemoryService(make_pool(settings.database_url), settings)
    print(ExtractionWorker(service).run())
