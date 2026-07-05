"""The API core (doc 07 §1): the single enforcement point.

Both facades (REST, MCP) route through this class; nothing reaches
Postgres except through it. Every public method opens one transaction, so
a write + its provenance + its events commit atomically (ADR-0004).

P1 surface (doc 07 §6): explicit_user_ask writes, recall + context block,
status introspection, episodes, scope tree + enrollment, event log
including READs, per-memory history and per-subject views.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from . import events, policy, retrieval, scopes
from .config import Settings
from .embedding import make_embedder
from .principals import Flow, Principal

KINDS = ("semantic", "episodic", "procedural", "profile")
ORIGIN_KINDS = ("explicit_user_ask", "llm_inferred", "agent_observed", "consolidated", "imported")

# Entry status by origin (doc 03 §2). P1 policy only lets the first row
# through; the table is complete so P2 doesn't have to touch this module.
ENTRY_STATUS = {
    "explicit_user_ask": "active",
    "llm_inferred": "staged",
    "agent_observed": "staged",
    "consolidated": "active",
    "imported": "staged",
}

# Provenance-derived trust (doc 06 §3). P1 keeps the simplest defensible
# map: explicit user directives are high-trust; everything else keeps the
# DDL default until the real derivation lands.
ORIGIN_TRUST = {"explicit_user_ask": 0.9}
ORIGIN_CONFIDENCE = {"explicit_user_ask": 0.9}


class AccessDenied(PermissionError):
    pass


class NotFound(LookupError):
    pass


class MemoryService:
    def __init__(self, pool, settings: Settings | None = None):
        self.pool = pool
        self.settings = settings or Settings()
        self.embedder = make_embedder(self.settings.embedder)
        self.policy_layers = [policy.P1_ORG_LAYER]

    # ---------- platform: scopes & enrollment ----------

    def create_scope(self, principal: Principal, **kw) -> dict:
        with self.pool.connection() as conn:
            return _plain(scopes.create_scope(conn.cursor(), **kw))

    def set_relation(
        self, principal: Principal, scope_id: str, relation: str, target: str, *, remove: bool = False
    ) -> None:
        """Enrollment and membership sync; every change is evented."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if scopes.get_scope(cur, scope_id) is None:
                raise NotFound(f"scope {scope_id!r} not found")
            if remove:
                cur.execute(
                    "DELETE FROM scope_relations WHERE scope_id=%s AND relation=%s AND principal=%s",
                    (scope_id, relation, target),
                )
            else:
                cur.execute(
                    "INSERT INTO scope_relations (scope_id, relation, principal)"
                    " VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (scope_id, relation, target),
                )
            self._event(
                cur, principal, "ENROLLMENT_CHANGE", scope_id=scope_id,
                details={"relation": relation, "principal": target,
                         "change": "remove" if remove else "add"},
            )

    # ---------- platform: episodes ----------

    def register_episode(
        self, principal: Principal, *, scope_id: str, source_kind: str,
        external_ref: dict, content: str | None = None, author: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if scopes.get_scope(cur, scope_id) is None:
                raise NotFound(f"scope {scope_id!r} not found")
            row = cur.execute(
                "INSERT INTO episodes (scope_id, source_kind, external_ref, content, author, occurred_at)"
                " VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (scope_id, source_kind, json.dumps(external_ref), content, author,
                 occurred_at or datetime.now(timezone.utc)),
            ).fetchone()
            return _plain(row)

    # ---------- write path ----------

    def remember(
        self, principal: Principal, flow: Flow, *, content: str, kind: str,
        origin_kind: str, subjects: list[str] | None = None,
        categories: list[str] | None = None, justification: str = "",
        source_episode_ids: list[str] | None = None,
        valid_at: datetime | None = None, sensitivity: str = "internal",
    ) -> dict:
        """The write pipeline (doc 05 §2). The response is the policy
        verdict, not a bare ack (doc 04 §1)."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if origin_kind not in ORIGIN_KINDS:
            raise ValueError(f"unknown origin_kind {origin_kind!r}")
        subjects = subjects or []
        categories = categories or []
        if not flow.container:
            raise ValueError("flow.container is required: writes route to the source scope")

        with self.pool.connection() as conn:
            cur = conn.cursor()
            target_scope = flow.container  # route: source (doc 05 §1)
            if scopes.get_scope(cur, target_scope) is None:
                raise NotFound(f"scope {target_scope!r} not found")

            # Enrollment gate first — an agent not enrolled as writer never
            # gets a policy 'allow'. Denials are events, not shrugs.
            if not scopes.writable(cur, principal, target_scope):
                verdict = policy.Verdict(
                    "deny", "access/enrollment",
                    f"{principal.actor} is not enrolled as a writer in {target_scope}", {},
                )
            else:
                verdict = policy.evaluate(
                    self.policy_layers, kind=kind, categories=categories, origin_kind=origin_kind
                )
            decision_event = self._event(
                cur, principal, "POLICY_DECISION", scope_id=target_scope,
                details={"verdict": verdict.decision, "rule_id": verdict.rule_id,
                         "layer_verdicts": verdict.layer_verdicts, "kind": kind,
                         "origin_kind": origin_kind, "categories": categories},
            )

            if verdict.decision == "deny":
                return {"decision": "deny", "memory_id": None, "status": None,
                        "ask_prompt": None, "reason": verdict.reason,
                        "rule_id": verdict.rule_id}
            if verdict.decision == "ask":
                # The confirm leg (memory_confirm + pending store) arrives in
                # P3; surfacing the prompt keeps the contract honest.
                return {"decision": "ask", "memory_id": None, "status": None,
                        "ask_prompt": f"Want me to remember: {content!r}?",
                        "reason": verdict.reason + " (confirmations arrive in P3; nothing stored)",
                        "rule_id": verdict.rule_id}

            episode_ids = list(source_episode_ids or [])
            if not episode_ids and origin_kind == "explicit_user_ask":
                # The ask is the episode (doc 03 §2): keep the provenance
                # chain unbroken even when the surface registered nothing.
                who = principal.effective_user or principal.actor
                ep = cur.execute(
                    "INSERT INTO episodes (scope_id, source_kind, external_ref, content, author, occurred_at)"
                    " VALUES (%s,'user_directive',%s,%s,%s,now()) RETURNING id",
                    (target_scope, json.dumps({"session": flow.session_id}), content, who),
                ).fetchone()
                episode_ids = [str(ep["id"])]
            for eid in episode_ids:
                ep = cur.execute("SELECT scope_id FROM episodes WHERE id=%s", (eid,)).fetchone()
                if ep is None:
                    raise NotFound(f"episode {eid!r} not found")
                if not scopes.readable(cur, principal, ep["scope_id"]):
                    raise AccessDenied(f"source episode {eid} is not visible to {principal.actor}")

            status = ENTRY_STATUS[origin_kind] if verdict.decision == "allow" else "staged"
            embedding = self.embedder.embed(content)
            mem = cur.execute(
                "INSERT INTO memories (kind, content, content_embedding, scope_id, subject_ids,"
                " categories, sensitivity, status, confidence, trust_score, valid_at)"
                " VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (kind, content, str(embedding) if embedding else None, target_scope, subjects,
                 categories, sensitivity, status,
                 ORIGIN_CONFIDENCE.get(origin_kind, 0.7), ORIGIN_TRUST.get(origin_kind, 0.5),
                 valid_at),
            ).fetchone()
            memory_id = str(mem["id"])
            cur.execute(
                "INSERT INTO memory_provenance (memory_id, origin_kind, responsible_agent,"
                " on_behalf_of, activity, justification)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (memory_id, origin_kind, principal.actor, principal.on_behalf_of,
                 json.dumps({"session": flow.session_id, "surface": flow.surface,
                             "policy_decision_id": str(decision_event["event_id"])}),
                 justification),
            )
            for eid in episode_ids:
                cur.execute(
                    "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                    " VALUES (%s,'episode',%s)",
                    (memory_id, eid),
                )
            self._event(
                cur, principal, "PROPOSE", memory_id=memory_id, scope_id=target_scope,
                details={"origin_kind": origin_kind, "entered_status": status,
                         "rule_id": verdict.rule_id},
            )
            return {"decision": verdict.decision, "memory_id": memory_id, "status": status,
                    "ask_prompt": None, "reason": verdict.reason, "rule_id": verdict.rule_id}

    # ---------- read paths ----------

    def recall(
        self, principal: Principal, flow: Flow, *, query: str,
        kinds: list[str] | None = None, subjects: list[str] | None = None,
        include_staged: bool = True, as_of: datetime | None = None, limit: int = 8,
    ) -> list[dict]:
        """Deliberate recall (doc 04 §2.2). Emits one READ event per delivery."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if as_of is not None and not scopes.is_auditor(cur, principal, self.settings.org_scope_id):
                raise AccessDenied("as-of queries are an audit feature (doc 05 §4.2)")
            chain = scopes.resolve_chain(cur, principal, flow)
            hits = retrieval.search(
                cur, scope_chain=chain, query=query,
                query_embedding=self.embedder.embed(query), settings=self.settings,
                kinds=kinds, subjects=subjects, include_staged=include_staged,
                as_of=as_of, limit=limit,
            )
            self._deliver(cur, principal, flow, [str(h["id"]) for h in hits],
                          path="deliberate", extra={"query": query})
            return [self._present(cur, h) for h in hits]

    def context_block(
        self, principal: Principal, flow: Flow, *, focus: str | None = None,
        token_budget: int = 1200,
    ) -> dict:
        """Ambient recall (doc 04 §2.1): invariants first, then active facts
        in scored order, then a clearly-fenced staged section."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            chain = scopes.resolve_chain(cur, principal, flow)
            hits = retrieval.search(
                cur, scope_chain=chain, query=focus,
                query_embedding=self.embedder.embed(focus) if focus else None,
                settings=self.settings, include_staged=True, limit=50,
            )
            invariants = [h for h in hits if h["status"] == "invariant"]
            facts = [h for h in hits if h["status"] == "active"]
            staged = [h for h in hits if h["status"] == "staged"]

            budget = token_budget * self.settings.chars_per_token
            container_label = self._scope_label(cur, flow.container) if flow.container else "-"
            now = datetime.now(timezone.utc)
            head = f'<memoramum scope="{container_label}" generated="{now:%Y-%m-%dT%H:%MZ}">'
            lines, delivered = [head], []

            def emit(section: str, rows: list[dict], fence: str | None = None) -> None:
                nonlocal budget
                if not rows or budget <= 0:
                    return
                header = section if fence is None else f"{section} ({fence})"
                lines.append(header)
                budget -= len(header)
                for h in rows:
                    line = f"- {h['content']} [{self._line_hint(cur, h)}]"
                    if budget - len(line) < 0:
                        return
                    lines.append(line)
                    budget -= len(line)
                    delivered.append(str(h["id"]))

            emit("INVARIANTS", invariants)
            emit("FACTS (current)", facts)
            emit("STAGED", staged, fence="unconfirmed — verify before relying on these")
            lines.append("</memoramum>")

            block_id = str(uuid.uuid4())
            self._deliver(cur, principal, flow, delivered, path="ambient",
                          extra={"context_block_id": block_id, "focus": focus,
                                 "token_budget": token_budget})
            return {"block_id": block_id, "block": "\n".join(lines), "memory_ids": delivered}

    def status(
        self, principal: Principal, *, memory_id: str | None = None,
        subject: str | None = None, scope: str | None = None,
    ) -> dict:
        """Read-only introspection (doc 04 §1 memory_status)."""
        if sum(x is not None for x in (memory_id, subject, scope)) != 1:
            raise ValueError("pass exactly one of memory_id, subject, scope")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if memory_id is not None:
                mem = self._get_readable_memory(cur, principal, memory_id)
                prov = cur.execute(
                    "SELECT * FROM memory_provenance WHERE memory_id=%s", (memory_id,)
                ).fetchone()
                sources = cur.execute(
                    "SELECT source_type, source_id FROM memory_derivations WHERE memory_id=%s",
                    (memory_id,),
                ).fetchall()
                digest = cur.execute(
                    "SELECT action, count(*) AS n, max(at) AS last_at FROM memory_events"
                    " WHERE memory_id=%s GROUP BY action ORDER BY max(at)",
                    (memory_id,),
                ).fetchall()
                return {
                    "memory": _plain(_public_memory(mem)),
                    "retention": retrieval.retention(mem, self.settings),
                    "provenance": _plain({k: prov[k] for k in
                                          ("origin_kind", "responsible_agent", "on_behalf_of",
                                           "justification")}) if prov else None,
                    "sources": _plain(sources),
                    "event_digest": _plain(digest),
                }
            if subject is not None:
                if not self._may_view_subject(cur, principal, subject):
                    raise AccessDenied(f"{principal.actor} may not enumerate memories about {subject}")
                rows = cur.execute(
                    "SELECT * FROM memories WHERE %s = ANY(subject_ids)"
                    " AND status NOT IN ('tombstoned') ORDER BY recorded_at DESC",
                    (subject,),
                ).fetchall()
                # DSAR posture (doc 06 §2): the subject (or an auditor) sees
                # everything ABOUT them, regardless of where it lives.
                return {"subject": subject, "memories": [_plain(_public_memory(r)) for r in rows]}
            assert scope is not None
            if not scopes.readable(cur, principal, scope):
                raise AccessDenied(f"{principal.actor} may not read scope {scope}")
            rows = cur.execute(
                "SELECT * FROM memories WHERE scope_id=%s AND status NOT IN ('tombstoned')"
                " ORDER BY recorded_at DESC",
                (scope,),
            ).fetchall()
            return {"scope": scope, "memories": [_plain(_public_memory(r)) for r in rows]}

    # ---------- platform/audit reads ----------

    def get_memory(self, principal: Principal, memory_id: str) -> dict:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            mem = self._get_readable_memory(cur, principal, memory_id)
            return _plain(_public_memory(mem))

    def memory_history(self, principal: Principal, memory_id: str) -> list[dict]:
        """The full event history of one memory (doc 04 §5) — the audit
        traversal of worked-scenario step 6 starts here."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            self._get_readable_memory(cur, principal, memory_id)
            rows = cur.execute(
                "SELECT seq, event_id, at, actor, on_behalf_of, action, scope_id, details"
                " FROM memory_events WHERE memory_id=%s ORDER BY seq",
                (memory_id,),
            ).fetchall()
            return _plain(rows)

    def subject_memories(self, principal: Principal, subject: str) -> dict:
        return self.status(principal, subject=subject)

    def scope_memories(self, principal: Principal, scope_id: str) -> dict:
        return self.status(principal, scope=scope_id)

    def audit_events(
        self, principal: Principal, *, action: str | None = None, actor: str | None = None,
        memory_id: str | None = None, scope_id: str | None = None, limit: int = 100,
    ) -> list[dict]:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not scopes.is_auditor(cur, principal, self.settings.org_scope_id):
                raise AccessDenied("event-log queries require the auditor relation on the org scope")
            where, params = ["true"], []
            for col, val in (("action", action), ("actor", actor),
                             ("memory_id", memory_id), ("scope_id", scope_id)):
                if val is not None:
                    where.append(f"{col} = %s")
                    params.append(val)
            rows = cur.execute(
                "SELECT seq, event_id, at, actor, on_behalf_of, action, memory_id, scope_id, details"
                f" FROM memory_events WHERE {' AND '.join(where)} ORDER BY seq DESC LIMIT {int(limit)}",
                params,
            ).fetchall()
            return _plain(rows)

    def get_episode(self, principal: Principal, episode_id: str) -> dict:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            row = cur.execute("SELECT * FROM episodes WHERE id=%s", (episode_id,)).fetchone()
            if row is None:
                raise NotFound(f"episode {episode_id!r} not found")
            if not (scopes.readable(cur, principal, row["scope_id"])
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                raise AccessDenied(f"{principal.actor} may not read episode {episode_id}")
            return _plain(row)

    # ---------- internals ----------

    def _event(self, cur, principal: Principal, action: str, **kw) -> dict:
        return events.append(
            cur, actor=principal.actor, on_behalf_of=principal.on_behalf_of, action=action,
            hash_chain_families=self.settings.hash_chain_families, **kw,
        )

    def _deliver(self, cur, principal: Principal, flow: Flow,
                 memory_ids: list[str], *, path: str, extra: dict) -> None:
        """Reads are events (ADR-0006): one READ per delivery, batched, plus
        the access-count/recency bookkeeping. Never mutates trust."""
        if not memory_ids:
            return
        cur.execute(
            "UPDATE memories SET last_accessed_at=now(), access_count=access_count+1"
            " WHERE id = ANY(%s::uuid[])",
            (memory_ids,),
        )
        self._event(
            cur, principal, "READ", scope_id=flow.container,
            details={"memory_ids": memory_ids, "path": path,
                     "surface": flow.surface, "session": flow.session_id, **extra},
        )

    def _get_readable_memory(self, cur, principal: Principal, memory_id: str) -> dict:
        mem = cur.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
        if mem is None:
            raise NotFound(f"memory {memory_id!r} not found")
        if not (scopes.readable(cur, principal, mem["scope_id"])
                or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
            raise AccessDenied(f"{principal.actor} may not read memory {memory_id}")
        return mem

    def _may_view_subject(self, cur, principal: Principal, subject: str) -> bool:
        # The subject themself, or an auditor (DSAR/review roles).
        return (principal.effective_user == subject
                or scopes.is_auditor(cur, principal, self.settings.org_scope_id))

    def _scope_label(self, cur, scope_id: str | None) -> str:
        if not scope_id:
            return "-"
        scope = scopes.get_scope(cur, scope_id)
        if scope and scope["external_ref"] and scope["external_ref"].get("name"):
            return scope["external_ref"]["name"]
        return scope_id

    def _provenance_hint(self, cur, memory_id: str, scope_id: str) -> str:
        """One line an agent can weigh and cite: origin, place, date, author,
        reinforcement count (doc 04 §1)."""
        prov = cur.execute(
            "SELECT origin_kind, responsible_agent, on_behalf_of FROM memory_provenance"
            " WHERE memory_id=%s", (memory_id,),
        ).fetchone()
        ep = cur.execute(
            "SELECT e.author, e.occurred_at, e.scope_id FROM memory_derivations d"
            " JOIN episodes e ON e.id = d.source_id"
            " WHERE d.memory_id=%s AND d.source_type='episode'"
            " ORDER BY e.occurred_at LIMIT 1", (memory_id,),
        ).fetchone()
        n_reinforce = cur.execute(
            "SELECT count(*) AS n FROM memory_events WHERE memory_id=%s AND action='REINFORCE'",
            (memory_id,),
        ).fetchone()["n"]
        verb = {"explicit_user_ask": "remembered at user request",
                "llm_inferred": "noted", "agent_observed": "observed",
                "consolidated": "consolidated", "imported": "imported"}.get(
                    prov["origin_kind"] if prov else "", "recorded")
        where = self._scope_label(cur, ep["scope_id"] if ep else scope_id)
        parts = [f"{verb} in {where}"]
        if ep:
            parts.append(f"{ep['occurred_at']:%Y-%m-%d}")
            if ep["author"]:
                parts.append(f"from {ep['author']}")
        hint = ", ".join(parts)
        if n_reinforce:
            hint += f"; reinforced {n_reinforce}×"
        return hint

    def _line_hint(self, cur, row: dict) -> str:
        scope = scopes.get_scope(cur, row["scope_id"])
        bits = []
        if row["status"] == "staged":
            bits.append("staged")
        if scope and scope["family"] == "subject":
            bits.append(f"about {row['scope_id'].removeprefix('subject:')}")
        else:
            bits.append(self._scope_label(cur, row["scope_id"]))
        bits.append(f"{row['recorded_at']:%b %Y}")
        if row["valid_at"]:
            end = f"{row['invalid_at']:%b %Y}" if row["invalid_at"] else "now"
            bits.append(f"valid {row['valid_at']:%b %Y}–{end}")
        return "; ".join(bits)

    def _present(self, cur, row: dict) -> dict:
        out = _plain(_public_memory(row))
        out["score"] = row.get("score")
        out["provenance"] = self._provenance_hint(cur, str(row["id"]), row["scope_id"])
        return out


def _public_memory(row: dict) -> dict:
    keys = ("id", "kind", "content", "scope_id", "subject_ids", "categories", "sensitivity",
            "status", "confidence", "trust_score", "strength", "access_count",
            "valid_at", "invalid_at", "recorded_at", "superseded_by", "expires_at")
    return {k: row[k] for k in keys if k in row}


def _plain(value: Any) -> Any:
    """JSON-safe copies of DB rows (UUIDs and datetimes → strings)."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value
