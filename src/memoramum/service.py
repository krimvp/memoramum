"""The API core (doc 07 §1): the single enforcement point.

Both facades (REST, MCP) route through this class; nothing reaches
Postgres except through it. Every public method opens one transaction, so
a write + its provenance + its events commit atomically (ADR-0004).

P1 surface (doc 07 §6): explicit_user_ask writes, recall + context block,
status introspection, episodes, scope tree + enrollment, event log
including READs, per-memory history and per-subject views.

P2 surface (doc 07 §6): the staged tier via llm_inferred hot-path writes,
reinforcement (memory_reinforce + write-time re-observation) with the
staged→active promotion rule, write-time contradiction handling
(supersede or hold, doc 03 §3), and the staged-triage / held-contradiction
review surface. The batch side — sweeps, escalation — is the
consolidator's (consolidator.py).

P3 surface (doc 07 §6): the full learning-policy engine — versioned
policy documents evaluated org → surface → agent → user-preference with
strictest-wins composition and policy-decided routing (doc 05 §1–2) —
`ask` flows backed by a pending-action store and `memory_confirm`, scope
promotion with confirmations and the doc 05 §3 gates, the PII pipeline
in the classify step (doc 06 §4), read-side sensitivity ceilings / trust
floors / category deny-lists (doc 05 §4.2), and policy administration
with simulation mode (doc 05 §5).

P4 surface (doc 07 §6): the remaining origin kinds through the same
write pipeline (`agent_observed` from the extraction workers,
`consolidated` with weakest-input entry tier and minimum-input trust,
doc 03 §2 / doc 06 §3), provenance-derived trust at write, memory_forget
(doc 03 §6 path 1), lineage quarantine with review (doc 06 §3), the
break-glass write-freeze (doc 05 §5), and the GDPR erasure pipeline with
signed attestations (doc 06 §2, erasure.py).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg

from . import erasure, events, lifecycle, observability, pii, policy, retrieval, scopes
from .config import Settings
from .embedding import make_embedder
from .principals import Flow, Principal

KINDS = ("semantic", "episodic", "procedural", "profile")
ORIGIN_KINDS = ("explicit_user_ask", "llm_inferred", "agent_observed", "consolidated", "imported")

# Micro-run promotions (doc 07 §2: "event-triggered micro-runs") act as the
# consolidator even when they fire inline on the write path.
CONSOLIDATOR = Principal("system:consolidator")

# Entry status by origin (doc 03 §2). `consolidated` additionally
# inherits the weakest input tier — staged if any source memory was
# staged (_enact_write).
ENTRY_STATUS = {
    "explicit_user_ask": "active",
    "llm_inferred": "staged",
    "agent_observed": "staged",
    "consolidated": "active",
    "imported": "staged",
}

ORIGIN_CONFIDENCE = {"explicit_user_ask": 0.9}

# Episode source kinds that count as untrusted tool output for trust
# derivation (doc 06 §3: "content that arrived via untrusted tool output
# gets a low base").
UNTRUSTED_SOURCE_KINDS = ("web_fetch", "tool_output", "forwarded")


class AccessDenied(PermissionError):
    pass


class NotFound(LookupError):
    pass


class PolicyUnavailable(Exception):
    """Doc 07 §5: the policy engine is unreachable. Writes fail closed —
    the caller queues and retries; nothing is stored without a verdict."""


class MemoryService:
    def __init__(self, pool, settings: Settings | None = None):
        self.pool = pool
        self.settings = settings or Settings()
        self.embedder = make_embedder(self.settings.embedder)
        self.judge = lifecycle.make_judge(self.settings.judge)
        self.analyzer = pii.make_analyzer(self.settings.pii_analyzer)

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

    def record_membership_sync(self, principal: Principal, *, surface: str) -> dict:
        """Membership-sync heartbeat (doc 05 §4.1, doc 07 §5): ingestion
        reports each completed sync of a surface's membership; retrieval
        holds reads to the staleness bound against this watermark."""
        if not surface:
            raise ValueError("surface is required")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not (principal.kind == "system" or self._is_org_admin(cur, principal)):
                raise AccessDenied(
                    "membership-sync heartbeats belong to platform ingestion (doc 07 §1)"
                )
            row = cur.execute(
                "INSERT INTO membership_sync (surface, synced_at) VALUES (%s, now())"
                " ON CONFLICT (surface) DO UPDATE SET synced_at = now()"
                " RETURNING surface, synced_at",
                (surface,),
            ).fetchone()
            return _plain(row)

    def set_module_paths(
        self, principal: Principal, *, module_scope_id: str, globs: list[str],
    ) -> dict:
        """Administer the ADR-0010 module-path mapping (org-admin or system):
        replace the glob set for a module scope. Replace, not merge — the
        stored set is the whole boundary. Operational config an admin owns
        (doc 07): a stale glob quietly routes new paths to the project scope,
        visible in staged-triage."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not (principal.kind == "system" or self._is_org_admin(cur, principal)):
                raise AccessDenied(
                    "module-path administration is an org-admin action (ADR-0010)"
                )
            scope = scopes.get_scope(cur, module_scope_id)
            if scope is None:
                raise NotFound(f"scope {module_scope_id!r} not found")
            if scope["family"] != "module":
                raise ValueError(f"{module_scope_id} is not a module scope")
            cur.execute("DELETE FROM module_paths WHERE module_scope_id=%s", (module_scope_id,))
            for glob in globs:
                cur.execute(
                    "INSERT INTO module_paths (module_scope_id, glob) VALUES (%s,%s)"
                    " ON CONFLICT DO NOTHING",
                    (module_scope_id, glob),
                )
            return {"module_scope_id": module_scope_id, "globs": list(globs)}

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
            # Dev-time episode registration (memory_observe, ADR-0012) is
            # self-reported: an agent may only register episodes in a scope it
            # may write to. Platform ingestion (system) and user directives
            # (user) keep their existing reach.
            if principal.kind == "agent" and not scopes.writable(cur, principal, scope_id):
                raise AccessDenied(
                    f"{principal.actor} is not enrolled as a writer in {scope_id}"
                )
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
        source_memory_ids: list[str] | None = None,
        valid_at: datetime | None = None, sensitivity: str = "internal",
    ) -> dict:
        """The write pipeline (doc 05 §2): classify → evaluate layers →
        emit POLICY_DECISION → enact. The response is the policy verdict,
        not a bare ack (doc 04 §1)."""
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind!r}")
        if origin_kind not in ORIGIN_KINDS:
            raise ValueError(f"unknown origin_kind {origin_kind!r}")
        subjects = subjects or []
        categories = list(categories or [])
        if not flow.container:
            raise ValueError("flow.container is required: writes route to the source scope")

        with self.pool.connection() as conn:
            cur = conn.cursor()
            source_scope = scopes.get_scope(cur, flow.container)
            if source_scope is None:
                raise NotFound(f"scope {flow.container!r} not found")

            # Classify (doc 05 §2): the PII scan enriches the categories the
            # policy matches on — detected credentials trip the org secrets
            # floor even when the agent supplied no category.
            entities = self.analyzer.analyze(content)
            if any(e.kind == "credential" for e in entities) and "credentials" not in categories:
                categories.append("credentials")

            layers = self._layers_for_write(cur, agent=principal.actor, surface=flow.surface,
                                            subjects=subjects)
            verdict = policy.evaluate(layers, policy.Candidate(
                kind=kind, categories=tuple(categories), origin_kind=origin_kind,
                subjects=tuple(subjects), participants=tuple(flow.participants),
                surface=flow.surface, scope_class=source_scope["trust_class"],
            ))

            # Break-glass (doc 05 §5): a frozen agent writes nothing,
            # whatever the layers say. Reads are unaffected.
            if verdict.decision != "deny" and self._frozen(cur, principal.actor):
                verdict = policy.Verdict(
                    "deny", "org/break-glass",
                    f"{principal.actor} is write-frozen (break-glass, doc 05 §5)",
                    verdict.layer_verdicts,
                )

            # Routing (doc 05 §1): policy, not the agent, decides where the
            # write lands; the requested scope is only ever narrowed.
            target_scope = flow.container
            if verdict.decision != "deny":
                if verdict.route_scope == "subject":
                    target_scope = self._route_subject(cur, subjects) or target_scope
                elif verdict.route_scope == "module":
                    target_scope = self._route_module(cur, flow) or target_scope
                elif verdict.route_scope == "agent":
                    target_scope = self._route_agent(cur, principal) or target_scope

            # Enrollment gate on the routed target — an agent not enrolled
            # as writer never gets a policy 'allow'. Denials are events.
            if not scopes.writable(cur, principal, target_scope):
                verdict = policy.Verdict(
                    "deny", "access/enrollment",
                    f"{principal.actor} is not enrolled as a writer in {target_scope}",
                    verdict.layer_verdicts,
                )

            # Per-entity PII actions against the routed target (doc 06 §4).
            pipeline = pii.run(
                cur, entities, content=content, target_scope_id=target_scope,
                subjects=subjects, effective_user=principal.effective_user,
                categories=categories,
            )
            if pipeline.blocked and verdict.decision != "deny":
                verdict = policy.Verdict("deny", "pii/block", pipeline.block_reason,
                                         verdict.layer_verdicts)

            decision_event = self._event(
                cur, principal, "POLICY_DECISION", scope_id=target_scope,
                details={"verdict": verdict.decision, "rule_id": verdict.rule_id,
                         "layer_verdicts": verdict.layer_verdicts, "kind": kind,
                         "origin_kind": origin_kind, "categories": categories,
                         "subjects": subjects, "participants": list(flow.participants),
                         "surface": flow.surface, "route": verdict.route_scope},
            )

            if verdict.decision == "deny":
                return {"decision": "deny", "memory_id": None, "status": None,
                        "ask_prompt": None, "reason": verdict.reason,
                        "rule_id": verdict.rule_id}
            if verdict.decision == "ask":
                # The two-step of doc 04 §1: park the candidate, hand the
                # agent the exact question to relay; memory_confirm closes it.
                pending = cur.execute(
                    "INSERT INTO pending_actions (action, payload, requested_by, on_behalf_of,"
                    " source_scope_id, target_scope_id, confirmer, ask_prompt,"
                    " policy_decision_id, rule_id)"
                    " VALUES ('remember',%s,%s,%s,%s,%s,'flow_user',%s,%s,%s) RETURNING id",
                    (json.dumps({
                        "content": content, "kind": kind, "origin_kind": origin_kind,
                        "subjects": subjects, "categories": categories,
                        "justification": justification,
                        "source_episode_ids": list(source_episode_ids or []),
                        "source_memory_ids": list(source_memory_ids or []),
                        "valid_at": valid_at.isoformat() if valid_at else None,
                        "sensitivity": sensitivity,
                        "surface": flow.surface, "container": flow.container,
                        "session_id": flow.session_id, "target_scope": target_scope,
                        "ttl_days": verdict.ttl_days,
                        "sensitivity_floor": verdict.sensitivity_floor,
                        "rule_id": verdict.rule_id,
                     }),
                     principal.actor, principal.on_behalf_of, flow.container, target_scope,
                     f"Want me to remember: {content!r}?",
                     str(decision_event["event_id"]), verdict.rule_id),
                ).fetchone()
                return {"decision": "ask", "memory_id": None, "status": None,
                        "pending_id": str(pending["id"]),
                        "ask_prompt": f"Want me to remember: {content!r}?",
                        "reason": verdict.reason, "rule_id": verdict.rule_id}

            return self._enact_write(
                cur, principal, flow, verdict=verdict,
                decision_event_id=str(decision_event["event_id"]),
                layers=layers, content=pipeline.content, pii_actions=pipeline.actions,
                kind=kind, origin_kind=origin_kind, subjects=subjects,
                categories=categories, sensitivity=sensitivity,
                justification=justification,
                source_episode_ids=list(source_episode_ids or []),
                source_memory_ids=list(source_memory_ids or []),
                valid_at=valid_at, target_scope=target_scope,
            )

    def _enact_write(
        self, cur, principal: Principal, flow: Flow, *, verdict, decision_event_id: str,
        layers: list, content: str, pii_actions: list, kind: str, origin_kind: str,
        subjects: list[str], categories: list[str], sensitivity: str, justification: str,
        source_episode_ids: list[str], valid_at: datetime | None, target_scope: str,
        source_memory_ids: list[str] | None = None, confirmed_by: str | None = None,
    ) -> dict:
        """The enactment half of the write pipeline — also run when a human
        approves a pending `ask` (then `confirmed_by` carries the human and
        the memory earns `active` through an explicit confirmation)."""
        episode_ids = list(source_episode_ids)
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
        source_memories = []
        for smid in source_memory_ids or []:
            src = cur.execute("SELECT * FROM memories WHERE id=%s", (smid,)).fetchone()
            if src is None:
                raise NotFound(f"source memory {smid!r} not found")
            if not scopes.readable(cur, principal, src["scope_id"]):
                raise AccessDenied(f"source memory {smid} is not visible to {principal.actor}")
            source_memories.append(src)

        status = ENTRY_STATUS[origin_kind] if verdict.decision == "allow" else "staged"
        if origin_kind == "consolidated" and any(m["status"] == "staged" for m in source_memories):
            # Consolidator output inherits the weakest input tier (doc 03 §2).
            status = "staged"
        chain = scopes.resolve_chain(cur, principal, flow)
        if target_scope not in chain:
            # Routed writes (subject scopes) join the dedup/contradiction
            # checks even when the flow chain would not have looked there.
            chain = [target_scope] + chain

        # Re-observation before insertion (doc 03 §4): a duplicate of an
        # existing memory reinforces it instead of piling on a copy.
        dup = self._find_duplicate(cur, chain, content)
        if dup is not None:
            return self._reobserve(
                cur, principal, flow, dup, origin_kind=origin_kind,
                episode_ids=episode_ids, verdict=verdict,
            )

        # Contradiction check against retrieved neighbors in the same
        # scope chain (doc 03 §3). Judging is LLM-shaped; the judge is a
        # pluggable seam (lifecycle.Judge) — the default 'exact' judge
        # finds no contradictions, leaving the 'wrong' signal and review
        # queue as the paths in.
        embedding = self.embedder.embed(content)
        # A consolidated successor does not contradict what it derives from.
        contradicted = self._find_contradiction(
            cur, chain, content, embedding,
            exclude_ids={str(m["id"]) for m in source_memories},
        )

        # Retention (doc 05 §1): the winning strategy's TTL, shortened by
        # any per-category retention override; sensitivity is raised to
        # the strongest matched floor.
        ttl_days = verdict.ttl_days
        override = policy.retention_override(layers, categories)
        if override and override["ttl_days"] is not None:
            ttl_days = (override["ttl_days"] if ttl_days is None
                        else min(ttl_days, override["ttl_days"]))
        expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)
                      if ttl_days is not None else None)
        if verdict.sensitivity_floor and (policy.sensitivity_rank(verdict.sensitivity_floor)
                                          > policy.sensitivity_rank(sensitivity)):
            sensitivity = verdict.sensitivity_floor

        trust = self._derive_trust(cur, origin_kind, episode_ids, source_memories)
        mem = cur.execute(
            "INSERT INTO memories (kind, content, content_embedding, scope_id, subject_ids,"
            " categories, sensitivity, status, confidence, trust_score, valid_at, expires_at)"
            " VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (kind, content, str(embedding) if embedding else None, target_scope, subjects,
             categories, sensitivity, status,
             ORIGIN_CONFIDENCE.get(origin_kind, 0.7), trust,
             valid_at, expires_at),
        ).fetchone()
        memory_id = str(mem["id"])
        activity = {"session": flow.session_id, "surface": flow.surface,
                    "policy_decision_id": decision_event_id,
                    "classifier": self.analyzer.version}
        if pii_actions:
            activity["pii_actions"] = pii_actions
        if flow.touched_paths:
            # The module hint (issue #15): which module scopes this write's
            # source paths touch, so staged-triage and promotion tooling can
            # propose mr→module promotion once the candidate earns
            # reinforcement (doc 03 §4). A hint, not a router — the candidate
            # itself stayed in its source scope.
            project = self._flow_project(cur, flow)
            touched = scopes.modules_touched(cur, project, flow.touched_paths) if project else []
            if touched:
                activity["modules_touched"] = touched
        cur.execute(
            "INSERT INTO memory_provenance (memory_id, origin_kind, responsible_agent,"
            " on_behalf_of, activity, justification)"
            " VALUES (%s,%s,%s,%s,%s,%s)",
            (memory_id, origin_kind, principal.actor, principal.on_behalf_of,
             json.dumps(activity), justification),
        )
        for eid in episode_ids:
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " VALUES (%s,'episode',%s)",
                (memory_id, eid),
            )
        for src in source_memories:
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " VALUES (%s,'memory',%s) ON CONFLICT DO NOTHING",
                (memory_id, src["id"]),
            )
            cur.execute(  # consolidated output keeps its inputs' episode provenance
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " SELECT %s, source_type, source_id FROM memory_derivations"
                " WHERE memory_id=%s AND source_type='episode' ON CONFLICT DO NOTHING",
                (memory_id, src["id"]),
            )

        propose_details = {"origin_kind": origin_kind, "entered_status": status,
                           "rule_id": verdict.rule_id}
        response = {"decision": verdict.decision, "memory_id": memory_id, "status": status,
                    "ask_prompt": None, "reason": verdict.reason, "rule_id": verdict.rule_id}

        hold = contradicted is not None and lifecycle.must_hold(status, trust, contradicted)
        if contradicted is not None:
            old_id = str(contradicted["id"])
            key = "contradicts" if hold else "supersedes"
            propose_details[key] = old_id
            response[key] = old_id

        self._event(
            cur, principal, "PROPOSE", memory_id=memory_id, scope_id=target_scope,
            details=propose_details,
        )

        if contradicted is not None and hold:
            # Held contradiction (doc 03 §3): staged/low-trust input must
            # not assassinate trusted memories. The challenger stays
            # staged with a contradicts marker; the pair goes to
            # consolidation review.
            cur.execute(
                "INSERT INTO contradiction_queue (contradicted_id, challenger_id, queued_by)"
                " VALUES (%s,%s,%s)",
                (old_id, memory_id, principal.actor),
            )
            response["reason"] += (
                f"; contradicts {contradicted['status']} memory {old_id} —"
                " held for consolidation review (doc 03 §3)"
            )
        elif contradicted is not None:
            self._supersede(cur, principal, contradicted, successor_id=memory_id,
                            invalid_at=self._evidence_time(cur, episode_ids))
            response["reason"] += f"; supersedes memory {old_id} (validity window closed)"

        if confirmed_by is not None:
            # An approved `ask` is an explicit confirmation (doc 03 §4):
            # CONFIRM by the human, and unheld staged entries earn active
            # on the spot.
            cur.execute(
                "UPDATE memories SET strength = strength + %s,"
                " confidence = greatest(confidence, 0.9), last_accessed_at = now()"
                " WHERE id=%s",
                (lifecycle.REINFORCEMENT["confirm"], memory_id),
            )
            self._event(cur, Principal(confirmed_by), "CONFIRM", memory_id=memory_id,
                        scope_id=target_scope,
                        details={"via": "ask", "relayed_by": principal.actor})
            if status == "staged" and not hold:
                row = cur.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
                self._promote(cur, Principal(confirmed_by), row)
                response["status"] = "active"
        return response

    def reinforce(
        self, principal: Principal, flow: Flow, *, memory_id: str, signal: str, note: str = "",
    ) -> dict:
        """memory_reinforce (doc 04 §1): explicit usefulness feedback.
        'useful' increments strength and resets decay; 'wrong' lowers
        confidence and queues contradiction review — no forget powers
        needed. Reinforcement never mutates trust (doc 04 §3)."""
        if signal not in lifecycle.AGENT_SIGNALS:
            raise ValueError(f"unknown signal {signal!r} (expected one of {lifecycle.AGENT_SIGNALS})")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            mem = self._get_readable_memory(cur, principal, memory_id)
            if mem["status"] not in ("staged", "active", "invariant"):
                raise ValueError(f"cannot reinforce a {mem['status']} memory")
            details = {"signal": signal, "note": note,
                       "surface": flow.surface, "session": flow.session_id}
            if signal == "useful":
                cur.execute(
                    "UPDATE memories SET strength = strength + %s, last_accessed_at = now()"
                    " WHERE id=%s",
                    (lifecycle.REINFORCEMENT["useful"], memory_id),
                )
            else:  # wrong: doubt is a review item, not an agent forget power
                cur.execute(
                    "UPDATE memories SET confidence = greatest(0.05, confidence - 0.2)"
                    " WHERE id=%s",
                    (memory_id,),
                )
                cur.execute(
                    "INSERT INTO contradiction_queue (contradicted_id, queued_by) VALUES (%s,%s)",
                    (memory_id, principal.actor),
                )
                details["queued"] = "contradiction_review"
            self._event(cur, principal, "REINFORCE", memory_id=memory_id,
                        scope_id=mem["scope_id"], details=details)
            if signal == "useful":
                self._maybe_promote(cur, memory_id)
            row = cur.execute(
                "SELECT status, strength, confidence FROM memories WHERE id=%s", (memory_id,)
            ).fetchone()
            return {"memory_id": memory_id, "signal": signal, **_plain(row)}

    # ---------- scope promotion (doc 03 §4, doc 05 §3) ----------

    def promote(
        self, principal: Principal, flow: Flow, *, memory_id: str, target_scope: str,
        justification: str = "",
    ) -> dict:
        """memory_promote (doc 04 §1): move a memory to a broader/other
        scope. Evaluated as a write into the destination plus the
        promotion gates of doc 05 §3 — almost always returns `ask`."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            mem = self._get_readable_memory(cur, principal, memory_id)
            if mem["status"] not in ("staged", "active", "invariant"):
                raise ValueError(f"cannot promote a {mem['status']} memory")
            source = scopes.get_scope(cur, mem["scope_id"])
            target = scopes.get_scope(cur, target_scope)
            if target is None:
                raise NotFound(f"scope {target_scope!r} not found")
            if target_scope == mem["scope_id"]:
                raise ValueError("the memory already lives in that scope")

            crossing = self._crossing(cur, source, target)
            prov = cur.execute(
                "SELECT origin_kind FROM memory_provenance WHERE memory_id=%s", (memory_id,)
            ).fetchone()
            layers = self._layers_for_write(cur, agent=principal.actor,
                                            surface=flow.surface or source["surface"],
                                            subjects=mem["subject_ids"])
            gate = policy.promotion_gate(layers, crossing=crossing,
                                         source_trust_class=source["trust_class"])
            # …plus the write-into-destination evaluation (doc 05 §3). A
            # 'stage' there means the content is learnable at the target;
            # promotion never re-stages, so it maps to allow.
            write_verdict = policy.evaluate(layers, policy.Candidate(
                kind=mem["kind"], categories=tuple(mem["categories"]),
                origin_kind=prov["origin_kind"] if prov else "llm_inferred",
                subjects=tuple(mem["subject_ids"]), participants=tuple(flow.participants),
                surface=target["surface"], scope_class=target["trust_class"],
            ))
            write_decision = {"stage": "allow"}.get(write_verdict.decision,
                                                    write_verdict.decision)
            if policy.stricter(gate.decision, write_decision) == gate.decision:
                decision, rule_id, reason = gate.decision, gate.rule_id, gate.reason
            else:
                decision, rule_id, reason = (write_decision, write_verdict.rule_id,
                                             write_verdict.reason)
            # The one tool where the agent names a scope: the target must be
            # on its writable set (doc 04 §1).
            if not scopes.writable(cur, principal, target_scope):
                decision, rule_id = "deny", "access/enrollment"
                reason = f"{principal.actor} is not enrolled as a writer in {target_scope}"
            if decision != "deny" and self._frozen(cur, principal.actor):
                decision, rule_id = "deny", "org/break-glass"
                reason = f"{principal.actor} is write-frozen (break-glass, doc 05 §5)"

            decision_event = self._event(
                cur, principal, "POLICY_DECISION", memory_id=memory_id, scope_id=target_scope,
                details={"promotion": True, "verdict": decision, "rule_id": rule_id,
                         "from": mem["scope_id"], "to": target_scope, "crossing": crossing,
                         "gate_rule": gate.rule_id,
                         "layer_verdicts": write_verdict.layer_verdicts},
            )
            response = {"decision": decision, "memory_id": memory_id,
                        "from": mem["scope_id"], "to": target_scope,
                        "ask_prompt": None, "pending_id": None,
                        "reason": reason, "rule_id": rule_id}
            if decision == "deny":
                return response
            if decision == "ask":
                prompt = (f"Share {mem['content']!r} beyond {self._scope_label(cur, mem['scope_id'])}"
                          f" into {self._scope_label(cur, target_scope)}?")
                pending = cur.execute(
                    "INSERT INTO pending_actions (action, payload, requested_by, on_behalf_of,"
                    " source_scope_id, target_scope_id, confirmer, ask_prompt,"
                    " policy_decision_id, rule_id)"
                    " VALUES ('promote',%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (json.dumps({"memory_id": memory_id, "target_scope": target_scope,
                                 "justification": justification}),
                     principal.actor, principal.on_behalf_of, mem["scope_id"], target_scope,
                     gate.confirmer or "scope_member", prompt,
                     str(decision_event["event_id"]), rule_id),
                ).fetchone()
                response.update(ask_prompt=prompt, pending_id=str(pending["id"]))
                return response
            enacted = self._enact_promotion(
                cur, principal, mem, target_scope,
                decision_event_id=str(decision_event["event_id"]),
            )
            response.update(enacted)
            return response

    def confirm_pending(
        self, principal: Principal, pending_id: str, *, approved: bool, note: str = "",
    ) -> dict:
        """memory_confirm (doc 04 §1) and the review-UI confirmation leg:
        a human resolves a pending `ask`. Only humans confirm (doc 01 §4)
        — an agent calls this with `on_behalf_of` carrying the user whose
        answer it relays, and the CONFIRM/REJECT event is the human's."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            p = cur.execute(
                "SELECT * FROM pending_actions WHERE id=%s", (pending_id,)
            ).fetchone()
            if p is None:
                raise NotFound(f"pending action {pending_id!r} not found")
            if p["resolved_at"] is not None:
                raise ValueError(f"pending action {pending_id} is already resolved")
            human = principal.effective_user
            if human is None:
                raise AccessDenied("only humans confirm ask verdicts (doc 01 §4)")
            self._require_confirmer(cur, human, p)
            cur.execute(
                "UPDATE pending_actions SET resolved_at=now(), approved=%s, resolved_by=%s,"
                " note=%s WHERE id=%s",
                (approved, human, note, pending_id),
            )
            payload = p["payload"]
            if not approved:
                # The user declined → REJECT event, nothing stored (doc 05 §6).
                self._event(
                    cur, Principal(human), "REJECT",
                    memory_id=payload.get("memory_id"), scope_id=p["target_scope_id"],
                    details={"via": "ask", "pending_id": str(pending_id),
                             "action": p["action"], "note": note,
                             "relayed_by": principal.actor if principal.kind == "agent" else None},
                )
                return {"pending_id": str(pending_id), "approved": False,
                        "action": p["action"], "memory_id": None, "status": None}

            requester = Principal(p["requested_by"], p["on_behalf_of"])
            if p["action"] == "remember":
                flow = Flow(surface=payload.get("surface"), container=payload.get("container"),
                            session_id=payload.get("session_id"))
                if not scopes.writable(cur, requester, p["target_scope_id"]):
                    raise AccessDenied(
                        f"{requester.actor} is no longer enrolled as a writer"
                        f" in {p['target_scope_id']}"
                    )
                entities = self.analyzer.analyze(payload["content"])
                pipeline = pii.run(
                    cur, entities, content=payload["content"],
                    target_scope_id=p["target_scope_id"], subjects=payload["subjects"],
                    effective_user=requester.effective_user, categories=payload["categories"],
                )
                if pipeline.blocked:
                    raise ValueError(f"the candidate no longer passes the PII pipeline:"
                                     f" {pipeline.block_reason}")
                layers = self._layers_for_write(cur, agent=requester.actor,
                                                surface=payload.get("surface"),
                                                subjects=payload["subjects"])
                verdict = policy.Verdict(
                    "ask", payload.get("rule_id", "ask"), "confirmed by the user", {},
                    ttl_days=payload.get("ttl_days"),
                    sensitivity_floor=payload.get("sensitivity_floor"),
                )
                out = self._enact_write(
                    cur, requester, flow, verdict=verdict,
                    decision_event_id=str(p["policy_decision_id"]), layers=layers,
                    content=pipeline.content, pii_actions=pipeline.actions,
                    kind=payload["kind"], origin_kind=payload["origin_kind"],
                    subjects=payload["subjects"], categories=payload["categories"],
                    sensitivity=payload.get("sensitivity", "internal"),
                    justification=payload.get("justification", ""),
                    source_episode_ids=payload.get("source_episode_ids") or [],
                    source_memory_ids=payload.get("source_memory_ids") or [],
                    valid_at=datetime.fromisoformat(payload["valid_at"])
                    if payload.get("valid_at") else None,
                    target_scope=p["target_scope_id"], confirmed_by=human,
                )
                return {"pending_id": str(pending_id), "approved": True, "action": "remember",
                        "memory_id": out["memory_id"], "status": out["status"]}

            if p["action"] == "forget":
                mem = cur.execute(
                    "SELECT * FROM memories WHERE id=%s", (payload["memory_id"],)
                ).fetchone()
                if mem is None or mem["scope_id"] != p["source_scope_id"]:
                    raise ValueError("the memory moved or vanished since the ask was issued")
                self._event(
                    cur, Principal(human), "CONFIRM", memory_id=payload["memory_id"],
                    scope_id=mem["scope_id"],
                    details={"via": "forget", "pending_id": str(pending_id), "note": note,
                             "relayed_by": principal.actor if principal.kind == "agent" else None},
                )
                out = self._enact_forget(cur, requester, mem, mode=payload["mode"],
                                         reason=payload.get("reason", ""), via="ask")
                return {"pending_id": str(pending_id), "approved": True, "action": "forget",
                        **out}

            mem = cur.execute(
                "SELECT * FROM memories WHERE id=%s", (payload["memory_id"],)
            ).fetchone()
            if mem is None or mem["scope_id"] != p["source_scope_id"]:
                raise ValueError("the memory moved or vanished since the ask was issued")
            if not scopes.writable(cur, requester, p["target_scope_id"]):
                raise AccessDenied(
                    f"{requester.actor} is no longer enrolled as a writer"
                    f" in {p['target_scope_id']}"
                )
            self._event(
                cur, Principal(human), "CONFIRM", memory_id=payload["memory_id"],
                scope_id=p["target_scope_id"],
                details={"via": "promotion", "pending_id": str(pending_id), "note": note,
                         "relayed_by": principal.actor if principal.kind == "agent" else None},
            )
            enacted = self._enact_promotion(
                cur, requester, mem, p["target_scope_id"],
                decision_event_id=str(p["policy_decision_id"]),
            )
            return {"pending_id": str(pending_id), "approved": True, "action": "promote",
                    **enacted}

    def pending_queue(self, principal: Principal, *, scope_id: str | None = None) -> list[dict]:
        """Open `ask` confirmations, for the review UI (doc 06 §1.2) and
        for users asking 'what is waiting on me'."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if scope_id is not None:
                self._require_reviewer(cur, principal, scope_id)
                where, params = "p.resolved_at IS NULL AND p.target_scope_id=%s", [scope_id]
            elif scopes.is_auditor(cur, principal, self.settings.org_scope_id):
                where, params = "p.resolved_at IS NULL", []
            else:
                user = principal.effective_user
                if user is None:
                    raise AccessDenied("pass a scope_id, or ask as a user or auditor")
                where, params = "p.resolved_at IS NULL AND p.on_behalf_of=%s", [user]
            rows = cur.execute(
                f"SELECT p.id, p.action, p.ask_prompt, p.requested_by, p.on_behalf_of,"
                f" p.source_scope_id, p.target_scope_id, p.confirmer, p.rule_id, p.created_at"
                f" FROM pending_actions p WHERE {where} ORDER BY p.created_at",
                params,
            ).fetchall()
            return _plain(rows)

    # ---------- review surface: staged triage & held contradictions ----------

    def staged_queue(self, principal: Principal, *, scope_id: str | None = None) -> list[dict]:
        """The staged-triage queue (doc 07 §6 P2): what a review UI lists.
        Scoped to what the reviewing human could see at the source."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if scope_id is None:
                if not scopes.is_auditor(cur, principal, self.settings.org_scope_id):
                    raise AccessDenied("the org-wide staged queue requires the auditor relation")
                where, params = "true", []
            else:
                self._require_reviewer(cur, principal, scope_id)
                where, params = "m.scope_id = %s", [scope_id]
            rows = cur.execute(
                f"""
                SELECT m.*, q.id AS contradiction_id, q.contradicted_id, q.escalated_at
                FROM memories m
                LEFT JOIN contradiction_queue q
                  ON q.challenger_id = m.id AND q.resolved_at IS NULL
                WHERE m.status = 'staged' AND {where}
                ORDER BY q.id IS NOT NULL DESC, m.recorded_at
                """,
                params,
            ).fetchall()
            out = []
            for r in rows:
                item = _plain(_public_memory(r))
                item["contradicts"] = _plain(r["contradicted_id"])
                item["provenance"] = self._provenance_hint(cur, str(r["id"]), r["scope_id"])
                item["modules_touched"] = self._modules_hint(cur, str(r["id"]))
                out.append(item)
            return out

    def review_staged(
        self, principal: Principal, memory_id: str, *, action: str, note: str = "",
    ) -> dict:
        """Explicit confirmation / rejection via the review UI (doc 03 §4).
        Confirmers are humans who are members of the memory's scope
        (doc 05 §3) or auditors."""
        if action not in ("confirm", "reject"):
            raise ValueError(f"unknown review action {action!r} (expected 'confirm' or 'reject')")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            mem = cur.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
            if mem is None:
                raise NotFound(f"memory {memory_id!r} not found")
            self._require_reviewer(cur, principal, mem["scope_id"])
            if mem["status"] != "staged":
                raise ValueError(f"only staged memories are triaged; this one is {mem['status']}")
            if action == "confirm":
                held = cur.execute(
                    "SELECT id FROM contradiction_queue WHERE challenger_id=%s"
                    " AND resolved_at IS NULL",
                    (memory_id,),
                ).fetchone()
                if held:
                    raise ValueError(
                        f"memory has a held contradiction (queue item {held['id']});"
                        " resolve it instead of confirming directly"
                    )
                cur.execute(
                    "UPDATE memories SET strength = strength + %s,"
                    " confidence = greatest(confidence, 0.9), last_accessed_at = now()"
                    " WHERE id=%s",
                    (lifecycle.REINFORCEMENT["confirm"], memory_id),
                )
                self._event(cur, principal, "CONFIRM", memory_id=memory_id,
                            scope_id=mem["scope_id"], details={"via": "review", "note": note})
                self._promote(cur, principal, mem)
                new_status = "active"
            else:
                cur.execute("UPDATE memories SET status='archived' WHERE id=%s", (memory_id,))
                cur.execute(
                    "UPDATE contradiction_queue SET resolved_at=now(), resolution='reject',"
                    " note=%s WHERE challenger_id=%s AND resolved_at IS NULL",
                    (note, memory_id),
                )
                self._event(cur, principal, "REJECT", memory_id=memory_id,
                            scope_id=mem["scope_id"], details={"via": "review", "note": note})
                new_status = "archived"
            return {"memory_id": memory_id, "action": action, "status": new_status}

    def contradictions(self, principal: Principal, *, include_resolved: bool = False) -> list[dict]:
        """The held-contradiction queue (doc 03 §3), for review."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            where = "" if include_resolved else "WHERE q.resolved_at IS NULL"
            rows = cur.execute(
                f"""
                SELECT q.*, old.content AS contradicted_content, old.status AS contradicted_status,
                       old.scope_id, ch.content AS challenger_content,
                       ch.status AS challenger_status
                FROM contradiction_queue q
                JOIN memories old ON old.id = q.contradicted_id
                LEFT JOIN memories ch ON ch.id = q.challenger_id
                {where} ORDER BY q.queued_at
                """,
            ).fetchall()
            auditor = scopes.is_auditor(cur, principal, self.settings.org_scope_id)
            visible = [
                r for r in rows
                if auditor or (principal.effective_user is not None
                               and scopes.user_can_see(cur, principal.effective_user, r["scope_id"]))
            ]
            return _plain(visible)

    def resolve_contradiction(
        self, principal: Principal, queue_id: str, *, resolution: str,
        invalid_at: datetime | None = None, note: str = "",
    ) -> dict:
        """Human resolution of a held contradiction (doc 07 §2): supersede,
        keep both with validity windows, or reject the challenger."""
        if resolution not in ("supersede", "keep_both", "reject"):
            raise ValueError(
                f"unknown resolution {resolution!r} (expected supersede | keep_both | reject)"
            )
        with self.pool.connection() as conn:
            cur = conn.cursor()
            q = cur.execute(
                "SELECT * FROM contradiction_queue WHERE id=%s", (queue_id,)
            ).fetchone()
            if q is None:
                raise NotFound(f"contradiction queue item {queue_id!r} not found")
            if q["resolved_at"] is not None:
                raise ValueError(f"queue item {queue_id} is already resolved ({q['resolution']})")
            old = cur.execute(
                "SELECT * FROM memories WHERE id=%s", (q["contradicted_id"],)
            ).fetchone()
            self._require_reviewer(cur, principal, old["scope_id"])
            challenger = None
            if q["challenger_id"] is not None:
                challenger = cur.execute(
                    "SELECT * FROM memories WHERE id=%s", (q["challenger_id"],)
                ).fetchone()
            if resolution in ("supersede", "keep_both") and challenger is None:
                raise ValueError(f"queue item {queue_id} has no challenger memory to {resolution}")

            details = {"resolution": resolution, "queue_id": str(queue_id), "note": note}
            if resolution == "reject":
                if challenger is not None and challenger["status"] == "staged":
                    cur.execute("UPDATE memories SET status='archived' WHERE id=%s",
                                (challenger["id"],))
                    self._event(cur, principal, "REJECT", memory_id=str(challenger["id"]),
                                scope_id=challenger["scope_id"], details=details)
            else:
                # The human accepted the challenger: explicit confirmation.
                cur.execute(
                    "UPDATE memories SET strength = strength + %s,"
                    " confidence = greatest(confidence, 0.9), last_accessed_at = now()"
                    " WHERE id=%s",
                    (lifecycle.REINFORCEMENT["confirm"], challenger["id"]),
                )
                self._event(cur, principal, "CONFIRM", memory_id=str(challenger["id"]),
                            scope_id=challenger["scope_id"], details=details)
                if challenger["status"] == "staged":
                    self._promote(cur, principal, challenger)
                if invalid_at is None:
                    src = [str(r["source_id"]) for r in cur.execute(
                        "SELECT source_id FROM memory_derivations"
                        " WHERE memory_id=%s AND source_type='episode'",
                        (challenger["id"],),
                    ).fetchall()]
                    invalid_at = self._evidence_time(cur, src)
                if resolution == "supersede":
                    self._supersede(cur, principal, old, successor_id=str(challenger["id"]),
                                    invalid_at=invalid_at)
                else:
                    # keep_both: both facts stand, with validity windows —
                    # the old one stays active but describes a fact that
                    # ended (doc 03 preamble), so it leaves current recall.
                    cur.execute("UPDATE memories SET invalid_at=%s WHERE id=%s",
                                (invalid_at, old["id"]))
                    self._event(cur, principal, "CONFIRM", memory_id=str(old["id"]),
                                scope_id=old["scope_id"],
                                details={**details, "invalid_at": invalid_at.isoformat()})
            cur.execute(
                "UPDATE contradiction_queue SET resolved_at=now(), resolution=%s, note=%s"
                " WHERE id=%s",
                (resolution, note, queue_id),
            )
            return {"queue_id": str(queue_id), "resolution": resolution,
                    "contradicted_id": str(q["contradicted_id"]),
                    "challenger_id": _plain(q["challenger_id"])}

    # ---------- forgetting & incident response (doc 03 §6, doc 06 §2–3) ----------

    def forget(
        self, principal: Principal, flow: Flow, *, memory_id: str, reason: str = "",
        mode: str = "archive",
    ) -> dict:
        """memory_forget (doc 04 §1) — hard-forget path 1 (doc 03 §6).
        Policy-gated: an agent alone may only forget memories in its own
        `agent:*` scope, or ones it authored that are still staged;
        anything else routes to `ask`. A user forget (directly, or relayed
        with on_behalf_of) archives by default, tombstones on request —
        and subject-scope memories always tombstone on explicit user
        request."""
        if mode not in ("archive", "tombstone"):
            raise ValueError(f"unknown mode {mode!r} (expected 'archive' or 'tombstone')")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            mem = self._get_readable_memory(cur, principal, memory_id)
            if mem["status"] == "tombstoned":
                raise ValueError("the memory is already tombstoned")
            scope = scopes.get_scope(cur, mem["scope_id"])
            prov = cur.execute(
                "SELECT responsible_agent FROM memory_provenance WHERE memory_id=%s",
                (memory_id,),
            ).fetchone()
            user = principal.effective_user

            decision, rule_id, why = "ask", "forget/ask-default", (
                "forgetting beyond the caller's own authority asks a scope member (doc 04 §1)"
            )
            if principal.kind == "agent" and self._frozen(cur, principal.actor):
                decision, rule_id = "deny", "org/break-glass"
                why = f"{principal.actor} is write-frozen (break-glass, doc 05 §5)"
            elif user is not None:
                subject_owner = (scope["family"] == "subject"
                                 and scopes._has_relation(cur, [mem["scope_id"]], "owner", user))
                allowed = (subject_owner or user in mem["subject_ids"]
                           or scopes.user_can_see(cur, user, mem["scope_id"])
                           or scopes.is_auditor(cur, principal, self.settings.org_scope_id))
                if not allowed:
                    raise AccessDenied(f"{user} may not forget memory {memory_id}")
                if subject_owner:
                    # doc 03 §6: subject-scope memories always tombstone on
                    # explicit user request.
                    mode = "tombstone"
                if mem["status"] == "invariant" and not scopes.is_auditor(
                        cur, principal, self.settings.org_scope_id):
                    rule_id = "forget/invariant"
                    why = "invariants change only by explicit privileged action (doc 03 §1)"
                else:
                    decision, rule_id = "allow", "forget/user"
                    why = "user forget (doc 03 §6 path 1)"
            else:  # an agent acting alone
                own_scope = mem["scope_id"] == principal.actor
                authored_staged = (prov is not None
                                   and prov["responsible_agent"] == principal.actor
                                   and mem["status"] == "staged")
                if own_scope or authored_staged:
                    decision, rule_id = "allow", "forget/own-authority"
                    why = ("the agent's own notebook" if own_scope
                           else "authored by the agent and still staged (doc 04 §1)")

            self._event(
                cur, principal, "POLICY_DECISION", memory_id=memory_id,
                scope_id=mem["scope_id"],
                details={"forget": True, "verdict": decision, "rule_id": rule_id,
                         "mode": mode, "reason": reason},
            )
            response = {"decision": decision, "memory_id": memory_id, "status": mem["status"],
                        "ask_prompt": None, "pending_id": None,
                        "reason": why, "rule_id": rule_id}
            if decision == "deny":
                return response
            if decision == "ask":
                prompt = f"Forget {mem['content']!r} from {self._scope_label(cur, mem['scope_id'])}?"
                pending = cur.execute(
                    "INSERT INTO pending_actions (action, payload, requested_by, on_behalf_of,"
                    " source_scope_id, target_scope_id, confirmer, ask_prompt, rule_id)"
                    " VALUES ('forget',%s,%s,%s,%s,%s,'scope_member',%s,%s) RETURNING id",
                    (json.dumps({"memory_id": memory_id, "mode": mode, "reason": reason}),
                     principal.actor, principal.on_behalf_of, mem["scope_id"], mem["scope_id"],
                     prompt, rule_id),
                ).fetchone()
                response.update(ask_prompt=prompt, pending_id=str(pending["id"]))
                return response
            enacted = self._enact_forget(cur, principal, mem, mode=mode, reason=reason)
            response.update(enacted)
            return response

    def quarantine(
        self, principal: Principal, *, episode_id: str | None = None,
        author: str | None = None, source_kind: str | None = None,
        agent: str | None = None, scope_id: str | None = None,
        occurred_from: datetime | None = None, occurred_to: datetime | None = None,
        note: str = "",
    ) -> dict:
        """Lineage quarantine (doc 06 §3): a source predicate → reverse
        derivation walk → mass QUARANTINE. Quarantined memories are
        excluded from all retrieval pending review (restore or tombstone);
        the provenance graph is the security control."""
        predicate = {k: v for k, v in {
            "episode_id": episode_id, "author": author, "source_kind": source_kind,
            "agent": agent, "scope_id": scope_id,
            "occurred_from": occurred_from.isoformat() if occurred_from else None,
            "occurred_to": occurred_to.isoformat() if occurred_to else None,
        }.items() if v is not None}
        if not predicate:
            raise ValueError("an empty predicate would quarantine everything; name a source")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            self._require_incident_role(cur, principal)

            seeds: set[str] = set()
            if episode_id or author or source_kind or scope_id or occurred_from or occurred_to:
                where, params = ["true"], []
                for col, val in (("e.id", episode_id), ("e.author", author),
                                 ("e.source_kind", source_kind), ("e.scope_id", scope_id)):
                    if val is not None:
                        where.append(f"{col} = %s")
                        params.append(val)
                if occurred_from is not None:
                    where.append("e.occurred_at >= %s")
                    params.append(occurred_from)
                if occurred_to is not None:
                    where.append("e.occurred_at <= %s")
                    params.append(occurred_to)
                seeds |= {str(r["memory_id"]) for r in cur.execute(
                    "SELECT DISTINCT d.memory_id FROM memory_derivations d"
                    " JOIN episodes e ON e.id = d.source_id"
                    f" WHERE d.source_type='episode' AND {' AND '.join(where)}",
                    params,
                ).fetchall()}
            if agent is not None:
                where, params = ["p.responsible_agent = %s"], [agent]
                if occurred_from is not None:
                    where.append("m.recorded_at >= %s")
                    params.append(occurred_from)
                if occurred_to is not None:
                    where.append("m.recorded_at <= %s")
                    params.append(occurred_to)
                seeds |= {str(r["memory_id"]) for r in cur.execute(
                    "SELECT p.memory_id FROM memory_provenance p"
                    " JOIN memories m ON m.id = p.memory_id"
                    f" WHERE {' AND '.join(where)}",
                    params,
                ).fetchall()}

            closure = erasure.derived_closure(cur, seeds)
            rows = cur.execute(
                "SELECT * FROM memories WHERE id = ANY(%s::uuid[])"
                " AND status IN ('staged','active','invariant','deprecated')"
                " ORDER BY recorded_at",
                (sorted(closure),),
            ).fetchall() if closure else []

            req = cur.execute(
                "INSERT INTO quarantine_requests (predicate, requested_by, note)"
                " VALUES (%s,%s,%s) RETURNING id",
                (json.dumps(predicate), principal.actor, note),
            ).fetchone()
            request_id = str(req["id"])
            for row in rows:
                cur.execute(
                    "INSERT INTO quarantine_items (request_id, memory_id) VALUES (%s,%s)",
                    (request_id, row["id"]),
                )
                self._event(
                    cur, principal, "QUARANTINE", memory_id=str(row["id"]),
                    scope_id=row["scope_id"],
                    details={"request_id": request_id, "predicate": predicate},
                )
            return {"request_id": request_id, "predicate": predicate,
                    "memory_ids": [str(r["id"]) for r in rows]}

    def quarantine_queue(self, principal: Principal, *, include_resolved: bool = False) -> list[dict]:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            self._require_incident_role(cur, principal)
            where = "" if include_resolved else "WHERE i.resolved_at IS NULL"
            rows = cur.execute(
                f"""
                SELECT i.request_id, i.memory_id, i.quarantined_at, i.resolved_at,
                       i.resolution, i.resolved_by, q.predicate, q.requested_by,
                       m.content, m.status, m.scope_id
                FROM quarantine_items i
                JOIN quarantine_requests q ON q.id = i.request_id
                JOIN memories m ON m.id = i.memory_id
                {where} ORDER BY i.quarantined_at, i.memory_id
                """,
            ).fetchall()
            return _plain(rows)

    def resolve_quarantine(
        self, principal: Principal, request_id: str, memory_id: str, *, action: str,
        note: str = "",
    ) -> dict:
        """Close one quarantined item after review (doc 06 §3): restore it
        to retrieval, or tombstone it."""
        if action not in ("restore", "tombstone"):
            raise ValueError(f"unknown action {action!r} (expected 'restore' or 'tombstone')")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            self._require_incident_role(cur, principal)
            item = cur.execute(
                "SELECT * FROM quarantine_items WHERE request_id=%s AND memory_id=%s",
                (request_id, memory_id),
            ).fetchone()
            if item is None:
                raise NotFound(f"no quarantine item for memory {memory_id!r} in request {request_id!r}")
            if item["resolved_at"] is not None:
                raise ValueError(f"the item is already resolved ({item['resolution']})")
            mem = cur.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
            if action == "restore":
                self._event(cur, principal, "RESTORE", memory_id=memory_id,
                            scope_id=mem["scope_id"],
                            details={"request_id": str(request_id), "note": note})
            else:
                self._tombstone(cur, principal, mem, f"quarantine review: {note or 'poisoned'}")
            cur.execute(
                "UPDATE quarantine_items SET resolved_at=now(), resolution=%s,"
                " resolved_by=%s, note=%s WHERE request_id=%s AND memory_id=%s",
                (action, principal.actor, note, request_id, memory_id),
            )
            return {"request_id": str(request_id), "memory_id": memory_id, "action": action}

    def set_agent_freeze(
        self, principal: Principal, agent: str, *, frozen: bool, reason: str = "",
    ) -> dict:
        """Break-glass (doc 05 §5): org admins can freeze an agent's writes
        entirely — one flag, evented, for incident response. Reads are
        unaffected."""
        if not agent.startswith("agent:"):
            raise ValueError("break-glass freezes agents (agent:*)")
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not self._is_org_admin(cur, principal):
                raise AccessDenied(
                    "break-glass requires the owner relation on the org scope (doc 05 §5)"
                )
            if frozen and not self._frozen(cur, agent):
                cur.execute(
                    "INSERT INTO agent_freezes (agent, frozen_by, reason) VALUES (%s,%s,%s)",
                    (agent, principal.actor, reason),
                )
            elif not frozen:
                cur.execute(
                    "UPDATE agent_freezes SET released_at=now(), released_by=%s"
                    " WHERE agent=%s AND released_at IS NULL",
                    (principal.actor, agent),
                )
            self._event(
                cur, principal, "POLICY_CHANGE", scope_id=self.settings.org_scope_id,
                details={"break_glass": True, "agent": agent, "frozen": frozen,
                         "reason": reason},
            )
            return {"agent": agent, "frozen": frozen}

    def request_erasure(
        self, principal: Principal, *, subject: str, legal_basis: str = "gdpr_art_17",
        note: str = "",
    ) -> dict:
        """The GDPR erasure pipeline (doc 06 §2.2), run synchronously in
        the reference implementation (production runs it as a job inside
        the doc 07 §4 SLOs). Initiated by the subject themself, or by
        admin/audit roles fulfilling a DSAR."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not (principal.effective_user == subject
                    or self._is_org_admin(cur, principal)
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                raise AccessDenied(
                    f"erasure of {subject} may be requested by the subject or admin/audit roles"
                )
            return erasure.run(self, cur, principal=principal, subject=subject,
                               legal_basis=legal_basis, note=note)

    def erasure_request(self, principal: Principal, request_id: str) -> dict:
        with self.pool.connection() as conn:
            cur = conn.cursor()
            row = cur.execute(
                "SELECT * FROM erasure_requests WHERE id=%s", (request_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"erasure request {request_id!r} not found")
            if not (principal.effective_user == row["subject"]
                    or self._is_org_admin(cur, principal)
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                raise AccessDenied("erasure records are for the subject and admin/audit roles")
            return _plain(row)

    # ---------- policy administration (doc 05 §5) ----------

    def put_policy(self, principal: Principal, document) -> dict:
        """Store a new version of a policy document (YAML in, canonical
        JSON out). Org admins (owner relation on the org scope) manage the
        org/surface/agent layers; a user manages their own user-preference
        layer (doc 05 §2). Every change is a POLICY_CHANGE event with a
        strategy-level diff."""
        parsed = policy.parse_document(document)
        with self.pool.connection() as conn:
            cur = conn.cursor()
            own_pref = (parsed["layer"] == "user_pref" and principal.kind == "user"
                        and principal.actor == parsed["applies_to"])
            if not own_pref and not self._is_org_admin(cur, principal):
                raise AccessDenied(
                    "policy administration requires the owner relation on the org scope"
                    " (users may set their own user_pref layer)"
                )
            prev = cur.execute(
                "SELECT version, document FROM policies WHERE layer=%s AND applies_to=%s"
                " ORDER BY version DESC LIMIT 1",
                (parsed["layer"], parsed["applies_to"]),
            ).fetchone()
            version = (prev["version"] + 1) if prev else 1
            cur.execute(
                "INSERT INTO policies (name, layer, applies_to, version, document, created_by)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (parsed["policy"], parsed["layer"], parsed["applies_to"], version,
                 json.dumps(parsed), principal.actor),
            )
            old_names = {s["name"] for s in
                         (prev["document"]["learning"]["strategies"] if prev else [])}
            new_names = {s["name"] for s in parsed["learning"]["strategies"]}
            self._event(
                cur, principal, "POLICY_CHANGE", scope_id=self.settings.org_scope_id,
                details={"policy": parsed["policy"], "layer": parsed["layer"],
                         "applies_to": parsed["applies_to"], "version": version,
                         "previous_version": prev["version"] if prev else None,
                         "diff": {"strategies_added": sorted(new_names - old_names),
                                  "strategies_removed": sorted(old_names - new_names)}},
            )
            return {"policy": parsed["policy"], "layer": parsed["layer"],
                    "applies_to": parsed["applies_to"], "version": version}

    def policies(self, principal: Principal) -> list[dict]:
        """The active (latest-version) policy documents."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT DISTINCT ON (layer, applies_to) name, layer, applies_to, version,"
                " document, created_by, created_at FROM policies"
                " ORDER BY layer, applies_to, version DESC",
            ).fetchall()
            if not (self._is_org_admin(cur, principal)
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                user = principal.effective_user
                rows = [r for r in rows
                        if r["layer"] == "user_pref" and r["applies_to"] == user]
            return _plain(rows)

    def simulate_policy(self, principal: Principal, document, *, days: int = 30) -> dict:
        """Simulation mode (doc 05 §5): evaluate a proposed policy against
        the last N days of POLICY_DECISION events and report what would
        change, before activation. Write decisions only — promotion gates
        and enrollment/PII denials are replayed as-is."""
        parsed = policy.parse_document(document)
        proposed = policy.layer_from_document(parsed, "proposed")
        override = (parsed["layer"], parsed["applies_to"], proposed)
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not (self._is_org_admin(cur, principal)
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                raise AccessDenied("policy simulation requires org-admin or auditor privileges")
            rows = cur.execute(
                "SELECT event_id, at, actor, details FROM memory_events"
                " WHERE action='POLICY_DECISION' AND at >= now() - make_interval(days => %s)"
                " AND details ? 'origin_kind' AND NOT (details ? 'promotion')"
                " ORDER BY at",
                (days,),
            ).fetchall()
            evaluated, changed = 0, []
            for r in rows:
                d = r["details"]
                rule = d.get("rule_id", "")
                if rule.startswith(("access/", "pii/")):
                    continue  # not a learning-policy outcome
                evaluated += 1
                layers = self._layers(cur, agent=r["actor"], surface=d.get("surface"),
                                      subjects=d.get("subjects") or [], override=override)
                new = policy.evaluate(layers, policy.Candidate(
                    kind=d["kind"], categories=tuple(d.get("categories") or ()),
                    origin_kind=d["origin_kind"],
                    subjects=tuple(d.get("subjects") or ()),
                    participants=tuple(d.get("participants") or ()),
                    surface=d.get("surface"),
                ))
                if new.decision != d.get("verdict"):
                    changed.append({"event_id": str(r["event_id"]), "at": r["at"].isoformat(),
                                    "actor": r["actor"], "old": d.get("verdict"),
                                    "new": new.decision, "old_rule": rule,
                                    "new_rule": new.rule_id})
            summary: dict[str, int] = {}
            for c in changed:
                key = f"{c['old']}->{c['new']}"
                summary[key] = summary.get(key, 0) + 1
            return {"policy": parsed["policy"], "layer": parsed["layer"],
                    "applies_to": parsed["applies_to"], "window_days": days,
                    "evaluated": evaluated, "changed": changed, "summary": summary}

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
            chain, rp, noted = self._degraded_read(cur, principal, flow, chain)
            hits = retrieval.search(
                cur, scope_chain=chain, query=query,
                query_embedding=self.embedder.embed(query), settings=self.settings,
                kinds=kinds, subjects=subjects,
                include_staged=include_staged and rp.include_staged,
                trust_floor=rp.trust_floor, sensitivity_ceiling=rp.sensitivity_ceiling,
                deny_categories=list(rp.deny_categories),
                as_of=as_of, limit=limit,
            )
            self._deliver(cur, principal, flow, [str(h["id"]) for h in hits],
                          path="deliberate", extra={"query": query, **noted})
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
            chain, rp, noted = self._degraded_read(cur, principal, flow, chain)
            hits = retrieval.search(
                cur, scope_chain=chain, query=focus,
                query_embedding=self.embedder.embed(focus) if focus else None,
                settings=self.settings, include_staged=rp.include_staged,
                trust_floor=rp.trust_floor, sensitivity_ceiling=rp.sensitivity_ceiling,
                deny_categories=list(rp.deny_categories), limit=50,
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
                                 "token_budget": token_budget, **noted})
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

    def metrics(self, principal: Principal, *, days: int = 30) -> dict:
        """The doc 07 §4 "metrics that matter", computed from the store
        (observability.py, ADR-0008). Reading them is reading operational
        aggregates over the whole org, so the gate matches the event log's:
        org-admin or auditor."""
        with self.pool.connection() as conn:
            cur = conn.cursor()
            if not (self._is_org_admin(cur, principal)
                    or scopes.is_auditor(cur, principal, self.settings.org_scope_id)):
                raise AccessDenied("metrics require org-admin or auditor privileges (doc 07 §4)")
            return observability.snapshot(cur, self.settings, days=days)

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

    # ---------- internals: policy, routing & promotion (docs 05/06) ----------

    def _layers(
        self, cur, *, agent: str, surface: str | None, subjects: list[str] | tuple = (),
        override: tuple | None = None,
    ) -> list[policy.PolicyLayer]:
        """The layer stack for one evaluation: org → surface → agent →
        user-preference (one per user subject — users tighten what is
        learned *about them*, doc 05 §2). Latest stored version wins;
        the org layer falls back to the built-in baseline. `override`
        swaps one (layer, applies_to) slot for simulation mode."""
        specs = [("org", self.settings.org_scope_id)]
        if surface:
            specs.append(("surface", f"surface:{surface}"))
        specs.append(("agent", agent))
        for s in subjects or ():
            if s.startswith("user:"):
                specs.append(("user_pref", s))
        layers: list[policy.PolicyLayer] = []
        for layer_name, applies_to in specs:
            if override and (layer_name, applies_to) == override[:2]:
                layers.append(override[2])
                continue
            row = cur.execute(
                "SELECT document, version FROM policies WHERE layer=%s AND applies_to=%s"
                " ORDER BY version DESC LIMIT 1",
                (layer_name, applies_to),
            ).fetchone()
            if row is not None:
                layers.append(policy.layer_from_document(row["document"], row["version"]))
            elif layer_name == "org":
                layers.append(policy.BUILTIN_ORG_LAYER)
            elif layer_name == "surface" and applies_to == "surface:ide":
                # The dev-time agent surface ships a built-in default when no
                # surface policy is stored (ADR-0012/0013), like the org layer.
                layers.append(policy.BUILTIN_IDE_SURFACE_LAYER)
        return layers

    def _layers_for_write(self, cur, **kw) -> list[policy.PolicyLayer]:
        """Doc 07 §5: policy engine unreachable → fail closed for writes.
        No layer stack, no verdict, nothing stored."""
        try:
            return self._layers(cur, **kw)
        except psycopg.Error as e:
            raise PolicyUnavailable(
                f"policy layers unreadable ({e}); write refused — queue and retry (doc 07 §5)"
            ) from e

    def _degraded_read(self, cur, principal: Principal, flow: Flow,
                       chain: list[str]) -> tuple[list[str], policy.ReadPolicy, dict]:
        """The doc 07 §5 read-side degradations, applied to a resolved chain.

        Membership sync stale beyond the bound: private surface scopes drop
        from the chain (fail closed), the rest serve with the staleness
        noted in the READ event. Policy engine unreachable: fail closed for
        reads beyond the principal's own agent scope, under the built-in
        org baseline. The returned dict rides into the READ event details.
        """
        noted: dict[str, Any] = {}
        stale = scopes.membership_staleness_seconds(cur, flow.surface)
        if stale is not None and stale > self.settings.membership_staleness_bound_seconds:
            chain = [s for s in chain if s not in scopes.synced_private(cur, chain)]
            noted["membership_staleness_seconds"] = round(stale)
        try:
            rp = self._read_policy(cur, principal, flow)
        except psycopg.Error:
            chain = [s for s in chain if s == principal.actor]
            rp = policy.read_policy([policy.BUILTIN_ORG_LAYER])
            noted["policy_degraded"] = True
        return chain, rp, noted

    def _read_policy(self, cur, principal: Principal, flow: Flow) -> policy.ReadPolicy:
        """The merged read-side attribute rules for this agent/surface
        (doc 05 §1 `read:`, §4.2)."""
        return policy.read_policy(
            self._layers(cur, agent=principal.actor, surface=flow.surface)
        )

    def _route_subject(self, cur, subjects: list[str]) -> str | None:
        """route: {scope: subject} (doc 05 §1) — the write lands in the
        subject scope. Only unambiguous: exactly one subject. The scope is
        provisioned on first routing; enrollment still gates the write."""
        if len(subjects) != 1:
            return None
        sid = scopes.subject_scope_id(subjects[0])
        if scopes.get_scope(cur, sid) is None:
            scopes.create_scope(cur, scope_id=sid, family="subject",
                                parent_scope_id=self.settings.org_scope_id)
            if subjects[0].startswith("user:"):
                cur.execute(
                    "INSERT INTO scope_relations (scope_id, relation, principal)"
                    " VALUES (%s,'owner',%s) ON CONFLICT DO NOTHING",
                    (sid, subjects[0]),
                )
        return sid

    def _flow_project(self, cur, flow: Flow) -> str | None:
        """The flow's project scope: named explicitly (dev-time flows) or
        detected structurally from the container (ADR-0009/0010)."""
        return flow.project or scopes.flow_project_scope(cur, flow.container)

    def _route_module(self, cur, flow: Flow) -> str | None:
        """route: {scope: module} (ADR-0013): dev-time codebase conventions
        land in the touched module scope. Exactly one touched module → that
        scope; anything else (ambiguous or no match) falls open to the flow's
        project scope when resolvable, else None (the write stays in its
        source scope). Fail-open to broader-but-correct, never to a wrong
        module (ADR-0010)."""
        project = self._flow_project(cur, flow)
        modules = scopes.modules_touched(cur, project, flow.touched_paths) if project else []
        if len(modules) == 1:
            return modules[0]
        return project

    def _route_agent(self, cur, principal: Principal) -> str | None:
        """route: {scope: agent} (ADR-0013): personal dev-time task tactics
        stay in the writing agent's own scope. Provisioned on first routing
        like subject scopes; enrollment still gates the write (the agent owns
        its own scope, scopes.writable)."""
        if principal.kind != "agent":
            return None
        sid = principal.actor
        if scopes.get_scope(cur, sid) is None:
            scopes.create_scope(cur, scope_id=sid, family="agent",
                                parent_scope_id=self.settings.org_scope_id)
        return sid

    def _crossing(self, cur, source: dict, target: dict) -> str:
        """Classify a promotion for the doc 05 §3 gate table."""
        if target["family"] == "subject":
            return "subject"
        if target["family"] == "module":
            return "module"  # mr / dev-session → module (ADR-0011)
        if source["family"] == "module" and any(
            s["id"] == target["id"] for s in scopes.ancestors(cur, source["id"])
        ):
            return "module_project"  # module → its ancestor project (ADR-0011)
        if any(s["id"] == source["id"] for s in scopes.ancestors(cur, target["id"])):
            return "narrowing"  # broader → narrower: the target sits under the source
        return "shared"

    def _enact_promotion(
        self, cur, principal: Principal, mem: dict, target_scope: str, *,
        decision_event_id: str,
    ) -> dict:
        """Move the memory. The write pipeline re-runs against the
        destination (doc 03 §4, doc 06 §4): if the destination demands a
        content transform (tokenization), the promoted form is a NEW
        memory derived from the original — content is immutable
        (ADR-0001) — and the original stays put; otherwise the memory
        itself moves scope, which is the normal case (doc 03 §7)."""
        memory_id = str(mem["id"])
        entities = self.analyzer.analyze(mem["content"])
        pipeline = pii.run(
            cur, entities, content=mem["content"], target_scope_id=target_scope,
            subjects=mem["subject_ids"], effective_user=None,
            categories=list(mem["categories"]),
        )
        if pipeline.blocked:
            raise ValueError(f"promotion blocked by the PII pipeline: {pipeline.block_reason}")
        details = {"from": mem["scope_id"], "to": target_scope,
                   "policy_decision_id": decision_event_id}
        if pipeline.actions:
            details["pii_actions"] = pipeline.actions
        if pipeline.content == mem["content"]:
            cur.execute("UPDATE memories SET scope_id=%s WHERE id=%s",
                        (target_scope, memory_id))
            cur.execute(
                "UPDATE memory_provenance SET activity = activity || %s WHERE memory_id=%s",
                (json.dumps({"promotion_policy_decision_id": decision_event_id}), memory_id),
            )
            promoted_id = memory_id
        else:
            embedding = self.embedder.embed(pipeline.content)
            row = cur.execute(
                "INSERT INTO memories (kind, content, content_embedding, scope_id, subject_ids,"
                " categories, sensitivity, status, confidence, trust_score, strength,"
                " valid_at, expires_at)"
                " VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (mem["kind"], pipeline.content, str(embedding) if embedding else None,
                 target_scope, mem["subject_ids"], mem["categories"], mem["sensitivity"],
                 mem["status"], mem["confidence"], mem["trust_score"], mem["strength"],
                 mem["valid_at"], mem["expires_at"]),
            ).fetchone()
            promoted_id = str(row["id"])
            prov = cur.execute(
                "SELECT * FROM memory_provenance WHERE memory_id=%s", (memory_id,)
            ).fetchone()
            cur.execute(
                "INSERT INTO memory_provenance (memory_id, origin_kind, responsible_agent,"
                " on_behalf_of, activity, justification)"
                " VALUES (%s,%s,%s,%s,%s,%s)",
                (promoted_id, prov["origin_kind"] if prov else "llm_inferred",
                 principal.actor, principal.on_behalf_of,
                 json.dumps({"promoted_from": memory_id,
                             "policy_decision_id": decision_event_id,
                             "classifier": self.analyzer.version,
                             "pii_actions": pipeline.actions}),
                 prov["justification"] if prov else ""),
            )
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " VALUES (%s,'memory',%s)",
                (promoted_id, memory_id),
            )
            cur.execute(  # the promoted form keeps the episode provenance
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " SELECT %s, source_type, source_id FROM memory_derivations"
                " WHERE memory_id=%s AND source_type='episode' ON CONFLICT DO NOTHING",
                (promoted_id, memory_id),
            )
            details["promoted_as"] = promoted_id
        self._event(cur, principal, "PROMOTE_SCOPE", memory_id=memory_id,
                    scope_id=target_scope, details=details)
        return {"memory_id": promoted_id, "from": mem["scope_id"], "to": target_scope,
                "transformed": promoted_id != memory_id}

    def _require_confirmer(self, cur, human: str, pending: dict) -> None:
        """Who may answer an `ask` (doc 05 §3): the user it was relayed to
        (writes), any member of the source scope (shared-scope
        promotions), or the subject (subject-scope promotions)."""
        kind = pending["confirmer"]
        if kind == "flow_user":
            expected = pending["on_behalf_of"]
            if expected is None:
                kind = "scope_member"  # agent-only flow: a scope member decides
            elif human == expected:
                return
            else:
                raise AccessDenied(
                    f"the ask was relayed to {expected}; {human} may not answer it"
                )
        if kind == "scope_member":
            scope = pending["source_scope_id"] or pending["target_scope_id"]
            if scopes.user_can_see(cur, human, scope):
                return
            raise AccessDenied(
                f"confirmers are members of {scope} (doc 05 §3); {human} is not"
            )
        if kind in ("module_owner", "maintainer"):
            # mr → module asks the module_owner tuple on the module scope;
            # module → project asks the maintainer tuple on the project scope
            # (ADR-0011). Both are the target scope of the pending promotion —
            # a table lookup like every other ReBAC question.
            target = pending["target_scope_id"]
            if scopes._has_relation(cur, [target], kind, human):
                return
            raise AccessDenied(
                f"only a {kind} of {target} confirms this crossing (ADR-0011); {human} is not"
            )
        # subject: dana confirms what is recorded about dana in shared view.
        target = pending["target_scope_id"]
        owner = cur.execute(
            "SELECT principal FROM scope_relations WHERE scope_id=%s AND relation='owner'",
            (target,),
        ).fetchall()
        owners = {r["principal"] for r in owner} or {target.removeprefix("subject:").replace("/", ":", 1)}
        if human not in owners:
            raise AccessDenied(f"only the subject confirms crossings into {target} (doc 05 §3)")

    def _is_org_admin(self, cur, principal: Principal) -> bool:
        return scopes._has_relation(
            cur, [self.settings.org_scope_id], "owner", principal.actor
        )

    def _tombstone(self, cur, principal: Principal, mem: dict, reason: str) -> None:
        """Hard-forget path 3 (doc 03 §6): content, embedding and (via the
        generated column) tsv physically go; a content-free TOMBSTONE
        event keeps the audit chain intact (doc 06 §2)."""
        cur.execute(
            "UPDATE memories SET status='tombstoned', content='', content_embedding=NULL"
            " WHERE id=%s",
            (mem["id"],),
        )
        self._event(cur, principal, "TOMBSTONE", memory_id=str(mem["id"]),
                    scope_id=mem["scope_id"], details={"from": mem["status"], "reason": reason})

    def _enact_forget(
        self, cur, principal: Principal, mem: dict, *, mode: str, reason: str,
        via: str | None = None,
    ) -> dict:
        """FORGET is the intent event; tombstoning additionally leaves the
        content-free TOMBSTONE marker (doc 06 §2)."""
        details = {"mode": mode, "reason": reason, "from": mem["status"]}
        if via:
            details["via"] = via
        self._event(cur, principal, "FORGET", memory_id=str(mem["id"]),
                    scope_id=mem["scope_id"], details=details)
        if mode == "tombstone":
            self._tombstone(cur, principal, mem, reason or "user forget")
            return {"memory_id": str(mem["id"]), "status": "tombstoned"}
        cur.execute("UPDATE memories SET status='archived' WHERE id=%s", (mem["id"],))
        return {"memory_id": str(mem["id"]), "status": "archived"}

    def _frozen(self, cur, actor: str) -> bool:
        return cur.execute(
            "SELECT 1 FROM agent_freezes WHERE agent=%s AND released_at IS NULL LIMIT 1",
            (actor,),
        ).fetchone() is not None

    def _require_incident_role(self, cur, principal: Principal) -> None:
        """Quarantine is an incident lever (doc 06 §3): org admins and
        auditors only."""
        if self._is_org_admin(cur, principal) or scopes.is_auditor(
                cur, principal, self.settings.org_scope_id):
            return
        raise AccessDenied(
            "quarantine tooling requires org-admin or auditor privileges (doc 06 §3)"
        )

    def _derive_trust(
        self, cur, origin_kind: str, episode_ids: list[str], source_memories: list[dict],
    ) -> float:
        """Provenance-derived trust at write (doc 06 §3): explicit user
        directives > scope-member authors > external/unattributed authors
        > untrusted tool output; consolidated inherits the minimum of its
        inputs. Values are implementation defaults — the ordering is the
        design."""
        if origin_kind == "explicit_user_ask":
            return 0.9
        if origin_kind == "consolidated":
            return min((m["trust_score"] for m in source_memories), default=0.5)
        if origin_kind == "imported":
            return 0.4
        if not episode_ids:
            return 0.5
        eps = cur.execute(
            "SELECT author, source_kind, scope_id FROM episodes WHERE id = ANY(%s::uuid[])",
            (episode_ids,),
        ).fetchall()
        if any(e["source_kind"] in UNTRUSTED_SOURCE_KINDS for e in eps):
            return 0.25
        if any(e["source_kind"] == "dev_observation" for e in eps):
            # Self-reported from a personal dev session (ADR-0012): no
            # platform-side subscriber verified it, so a modest penalty below
            # a platform-attributed scope-member author (0.6).
            return 0.45
        if any(e["author"] is None
               or not scopes.user_can_see(cur, e["author"], e["scope_id"]) for e in eps):
            return 0.35
        return 0.6

    def _consolidated_insert(
        self, cur, principal: Principal, inputs: list[dict], *, scope_id: str,
        content: str, kind: str, job: str, justification: str,
        extra_activity: dict | None = None,
    ) -> str:
        """Direct consolidated successor for the mechanical jobs (dedupe
        merge, erasure regeneration): weakest input tier, minimum input
        trust (doc 03 §2, doc 06 §3), union of subjects / categories /
        episode provenance. Reflection candidates do NOT come through
        here — they go through the write pipeline like any agent's
        (doc 07 §2)."""
        subjects = sorted({s for m in inputs for s in m["subject_ids"]})
        categories = sorted({c for m in inputs for c in m["categories"]})
        sensitivity = max((m["sensitivity"] for m in inputs), key=policy.sensitivity_rank)
        status = ("staged" if any(m["status"] == "staged" for m in inputs) else "active")
        expires = [m["expires_at"] for m in inputs if m["expires_at"] is not None]
        valid = [m["valid_at"] for m in inputs if m["valid_at"] is not None]
        embedding = self.embedder.embed(content)
        row = cur.execute(
            "INSERT INTO memories (kind, content, content_embedding, scope_id, subject_ids,"
            " categories, sensitivity, status, confidence, trust_score, strength,"
            " valid_at, expires_at)"
            " VALUES (%s,%s,%s::vector,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (kind, content, str(embedding) if embedding else None, scope_id, subjects,
             categories, sensitivity, status,
             max(m["confidence"] for m in inputs),
             min(m["trust_score"] for m in inputs),
             sum(m["strength"] for m in inputs),
             min(valid) if valid else None, min(expires) if expires else None),
        ).fetchone()
        successor_id = str(row["id"])
        activity = {"job": job, "rule": f"{job}-v1",
                    "inputs": [str(m["id"]) for m in inputs], **(extra_activity or {})}
        cur.execute(
            "INSERT INTO memory_provenance (memory_id, origin_kind, responsible_agent,"
            " on_behalf_of, activity, justification)"
            " VALUES (%s,'consolidated',%s,%s,%s,%s)",
            (successor_id, principal.actor, principal.on_behalf_of,
             json.dumps(activity), justification),
        )
        for m in inputs:
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " VALUES (%s,'memory',%s) ON CONFLICT DO NOTHING",
                (successor_id, m["id"]),
            )
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " SELECT %s, source_type, source_id FROM memory_derivations"
                " WHERE memory_id=%s AND source_type='episode' ON CONFLICT DO NOTHING",
                (successor_id, m["id"]),
            )
        self._event(
            cur, principal, "PROPOSE", memory_id=successor_id, scope_id=scope_id,
            details={"origin_kind": "consolidated", "entered_status": status, "job": job},
        )
        return successor_id

    # ---------- internals: lifecycle (doc 03) ----------

    def _find_duplicate(self, cur, chain: list[str], content: str) -> dict | None:
        """An existing live memory in the chain with the same normalized
        content; the narrowest scope wins."""
        if not chain:
            return None
        return cur.execute(
            "SELECT * FROM memories WHERE scope_id = ANY(%s)"
            " AND status IN ('staged','active','invariant')"
            " AND lower(btrim(regexp_replace(content, '\\s+', ' ', 'g'))) = %s"
            " ORDER BY array_position(%s::text[], scope_id) LIMIT 1",
            (chain, lifecycle.normalize(content), chain),
        ).fetchone()

    def _reobserve(
        self, cur, principal: Principal, flow: Flow, dup: dict, *,
        origin_kind: str, episode_ids: list[str], verdict,
    ) -> dict:
        """The write became a reinforcement (doc 03 §4): an explicit ask
        that restates a known memory confirms it; anything else counts as a
        re-observation — but only from an independent episode."""
        dup_id = str(dup["id"])
        confirm = origin_kind == "explicit_user_ask"
        if not confirm and not lifecycle.independent_evidence(cur, dup_id, episode_ids):
            return {"decision": verdict.decision, "memory_id": dup_id, "status": dup["status"],
                    "ask_prompt": None, "rule_id": verdict.rule_id, "deduplicated": True,
                    "reason": "already known; not an independent re-observation (doc 03 §4)"}
        for eid in episode_ids:  # reinforcement events name their episodes (doc 06 §3)
            cur.execute(
                "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
                " VALUES (%s,'episode',%s) ON CONFLICT DO NOTHING",
                (dup_id, eid),
            )
        signal = "confirm" if confirm else "re_observation"
        cur.execute(
            "UPDATE memories SET strength = strength + %s, last_accessed_at = now(),"
            " confidence = greatest(confidence, %s) WHERE id=%s",
            (lifecycle.REINFORCEMENT[signal], 0.9 if confirm else 0.0, dup_id),
        )
        action = "CONFIRM" if confirm else "REINFORCE"
        self._event(
            cur, principal, action, memory_id=dup_id, scope_id=dup["scope_id"],
            details={"signal": signal, "episodes": episode_ids,
                     "surface": flow.surface, "session": flow.session_id},
        )
        status = dup["status"]
        if status == "staged" and self._maybe_promote(cur, dup_id):
            status = "active"
        return {"decision": verdict.decision, "memory_id": dup_id, "status": status,
                "ask_prompt": None, "rule_id": verdict.rule_id, "deduplicated": True,
                "reason": f"duplicate of an existing memory: reinforced ({signal}, doc 03 §4)"}

    def _find_contradiction(
        self, cur, chain: list[str], content: str, embedding: list[float] | None,
        exclude_ids: set[str] = frozenset(),
    ) -> dict | None:
        neighbors = retrieval.search(
            cur, scope_chain=chain, query=content, query_embedding=embedding,
            settings=self.settings, include_staged=True, trust_floor=0.0, limit=8,
        )
        for n in neighbors:
            if str(n["id"]) in exclude_ids:
                continue
            if self.judge.judge(content, n["content"]) == "contradiction":
                return n
        return None

    def _supersede(
        self, cur, principal: Principal, old: dict, *, successor_id: str, invalid_at: datetime,
    ) -> None:
        """Close the validity window and link the successor (doc 03 §3):
        content is never edited on contradiction (ADR-0001)."""
        old_id = str(old["id"])
        cur.execute(
            "UPDATE memories SET status='deprecated', invalid_at=%s, superseded_by=%s"
            " WHERE id=%s",
            (invalid_at, successor_id, old_id),
        )
        cur.execute(  # the successor derives from the contradicted memory too
            "INSERT INTO memory_derivations (memory_id, source_type, source_id)"
            " VALUES (%s,'memory',%s) ON CONFLICT DO NOTHING",
            (successor_id, old_id),
        )
        self._event(
            cur, principal, "SUPERSEDE", memory_id=old_id, scope_id=old["scope_id"],
            details={"superseded_by": successor_id, "invalid_at": invalid_at.isoformat()},
        )

    def _evidence_time(self, cur, episode_ids: list[str]) -> datetime:
        """invalid_at default: the new evidence's occurred_at (doc 03 §3)."""
        if episode_ids:
            row = cur.execute(
                "SELECT max(occurred_at) AS t FROM episodes WHERE id = ANY(%s::uuid[])",
                (episode_ids,),
            ).fetchone()
            if row["t"] is not None:
                return row["t"]
        return datetime.now(timezone.utc)

    def _maybe_promote(self, cur, memory_id: str) -> bool:
        """Micro-run of the consolidator's status-promotion job
        (doc 07 §2: event-triggered micro-runs on reinforcement)."""
        mem = cur.execute("SELECT * FROM memories WHERE id=%s", (memory_id,)).fetchone()
        if mem is None or not lifecycle.promotion_due(cur, mem, self.settings):
            return False
        self._promote(cur, CONSOLIDATOR, mem)
        return True

    def _promote(self, cur, principal: Principal, mem: dict) -> None:
        cur.execute("UPDATE memories SET status='active' WHERE id=%s", (mem["id"],))
        self._event(
            cur, principal, "PROMOTE_STATUS", memory_id=str(mem["id"]),
            scope_id=mem["scope_id"], details={"from": mem["status"], "to": "active"},
        )

    def _archive(self, cur, principal: Principal, mem: dict, reason: str) -> None:
        cur.execute("UPDATE memories SET status='archived' WHERE id=%s", (mem["id"],))
        self._event(
            cur, principal, "ARCHIVE", memory_id=str(mem["id"]), scope_id=mem["scope_id"],
            details={"from": mem["status"], "reason": reason},
        )

    def _require_reviewer(self, cur, principal: Principal, scope_id: str) -> None:
        """Triage and confirmations are human actions: the reviewing user
        must be able to see the scope (doc 05 §3 confirmers) or hold the
        auditor relation. Agents relay `ask` prompts and answers
        (confirm_pending); the decision recorded is the human's."""
        if scopes.is_auditor(cur, principal, self.settings.org_scope_id):
            return
        user = principal.effective_user
        if principal.kind == "user" and user and scopes.user_can_see(cur, user, scope_id):
            return
        raise AccessDenied(
            f"{principal.actor} may not review memories in {scope_id}:"
            " confirmers are scope members or auditors (doc 05 §3)"
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

    def _modules_hint(self, cur, memory_id: str) -> list[str]:
        """The module scopes this staged candidate's source paths touched
        (issue #15), recorded at write time in provenance activity — the hint
        the staged-triage UI shows to propose mr→module promotion."""
        prov = cur.execute(
            "SELECT activity FROM memory_provenance WHERE memory_id=%s", (memory_id,)
        ).fetchone()
        if prov and prov["activity"]:
            return list(prov["activity"].get("modules_touched") or [])
        return []

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
