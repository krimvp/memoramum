# 03 — Lifecycle

How a memory is born, earns trust, ages, and dies. This is the design's answer to "different types of memories or learnings (staging, consolidated, invariant, stale…)" and to "when to clean up things if never accessed."

Two orthogonal time dimensions govern everything here:

- **Lifecycle status** — the system's *operational* stance toward the memory (`staged` … `tombstoned`).
- **Bi-temporal validity** — when the fact was *true in the world* (`valid_at`/`invalid_at`) vs when the system *knew about it* (`recorded_at`/supersession). A memory can be `active` yet describe a fact that ended (historical queries), and a fact can be current yet `staged` (not yet trusted).

---

## 1. The state machine

```
                      explicit_user_ask ────────────────┐
                                                        ▼
 PROPOSE ──▶ staged ──reinforcement / confirm──▶ active ──pin──▶ invariant
               │                                   │  ▲              │
               │ decay / TTL / reject              │  └─── unpin ────┘
               ▼                                   │
            archived ◀───────── decay / TTL ───────┤
               │                                   │ contradiction / supersession
               │ erasure                           ▼
               ▼                                deprecated ──▶ archived ──▶ (erasure)
           tombstoned ◀────────────────────────────────────────────────────────┘
```

| Status | Meaning | Retrievable? |
|---|---|---|
| `staged` | Proposed by an agent/extraction, not yet trusted. Quarantine buffer. | Yes — but rank-penalized, labeled `[staged]` in context blocks, and excluded from high-stakes read contexts by policy trust floors ([doc 05 §4](05-policy.md)). |
| `active` | Consolidated, trusted, current. The normal tier. | Yes. |
| `invariant` | Pinned: org rules, hard constraints, standing user directives ("never DM the CEO"). Exempt from decay and from consolidator rewrites; changed only by explicit privileged action. | Yes — always included in ambient recall for their scope, budget permitting first. |
| `deprecated` | Superseded or invalidated. Not part of current state. | Only via history/as-of queries; never in default recall. |
| `archived` | Decayed/expired out of the working set. | Only via explicit history queries; candidate for cold storage. |
| `tombstoned` | Hard-erased; content gone, marker remains. | Never; the tombstone event is what audit sees. |

## 2. Birth: how origin determines the entry point

| `origin_kind` | Enters as | Rationale |
|---|---|---|
| `explicit_user_ask` | `active` | The user said "remember this." Friction here erodes the feature; auditability is already perfect (the ask is the episode). |
| `llm_inferred` (hot path) | `staged` | Agent judgment mid-conversation. Useful immediately, trusted later. |
| `agent_observed` (background extraction) | `staged` | Same, plus extraction over third-party messages is the main poisoning surface. |
| `consolidated` | `active` (from active inputs) / `staged` (if any input was staged) | Consolidator output inherits the weakest input tier. |
| `imported` | policy-defined, default `staged` | Bulk imports are unaudited by definition. |

Per-agent policy can tighten any of these (e.g. `ask` mode: every `llm_inferred` write requires user confirmation — [doc 05 §2](05-policy.md)). The **default** is the staged/auto-promote model ([ADR-0003](adr/0003-staged-tier-default.md)): silent enough to be usable, quarantined enough to be safe.

## 3. Bi-temporal validity and supersession

Adopted from Zep/Graphiti ([ADR-0001](adr/0001-supersede-dont-overwrite.md)): **facts are never edited or deleted on contradiction; their validity window is closed and a successor is linked.**

The four timestamps, on the worked scenario:

| Field | Tuesday-deploys memory |
|---|---|
| `valid_at` | ~2026-03-03 — when the Tuesday-only rule was (as far as we know) in force |
| `recorded_at` | 2026-03-03 — when Sage learned it |
| `invalid_at` | 2026-06-10 — when the world changed ("daily deploys now") |
| supersession (event + `superseded_by`) | 2026-06-10 — when the system *learned* it changed |

`valid_at`/`invalid_at` can lag or lead `recorded_at` arbitrarily ("we switched to daily deploys *last month*" sets `invalid_at` in the past). This is what makes three query classes possible:

- **Current state** (default recall): `status IN ('active','invariant','staged')` — i.e. validity-open memories.
- **As-of queries**: "what did we believe on 2026-04-01?" — filter on `recorded_at`/supersession events.
- **World-history queries**: "when did Atlas deploy weekly?" — filter on `valid_at`/`invalid_at`.

**Contradiction handling**: when a new candidate memory semantically conflicts with an existing one in the same scope chain (detected at write time against retrieved neighbors, and continuously by the consolidator — [doc 07 §2](07-operations.md)), the system: (1) creates the successor with `derived_from` pointing at both the new episode *and* the contradicted memory; (2) sets the old memory's `invalid_at` (LLM-extracted from the new evidence, defaulting to the new episode's `occurred_at`); (3) sets `status='deprecated'` and `superseded_by`; (4) emits `SUPERSEDE`. If the *contradicting* claim is staged/low-trust and the existing memory is active/high-trust, the contradiction is **held**: the new memory stays staged with a `contradicts` marker and the pair is queued for consolidation review rather than auto-superseding — staged input must not be able to assassinate trusted memories (poisoning defense, [doc 06 §3](06-audit-privacy-security.md)).

## 4. Promotion

Two distinct promotions, both explicit, both evented:

- **Status promotion** (`staged → active`): earned by **reinforcement** —
  - *re-observation*: extraction produces a duplicate of a staged memory from an independent episode (different author or different day);
  - *useful retrieval*: the memory was delivered into context and the agent/user signal marked it useful (explicit `memory_reinforce` call, or implicit: it was cited in an accepted answer);
  - *unchallenged tenure*: configurable — N days staged with ≥1 retrieval and no contradiction;
  - *explicit confirmation*: a user confirms an `ask` prompt or approves via the review UI.

  Default rule (per-category tunable): promote on `(re-observations ≥ 1) OR (useful retrievals ≥ 2) OR explicit confirm`, with a floor of 3 days in staging for `agent_observed` memories from non-member authors.
- **Scope promotion** (`channel → workspace`, `channel → subject:*`, anything → cross-surface-visible): governed by [doc 05 §3](05-policy.md). Crossing into a shared scope or a subject scope defaults to `ask` (a human confirms); promotions out of `private` trust-class scopes are denied by default. Scope promotion *re-runs the write pipeline* (redaction, sensitivity, policy) against the destination scope — a memory acceptable in `#deploys` may need PII tokenization to sit at workspace level. Monorepo crossings work the same way: `mr → module` (or dev-session → module) and the stricter `module → project` are governed crossings with their own confirmers ([doc 05 §3](05-policy.md), [ADR-0011](adr/0011-module-promotion-crossings.md)).

## 5. Aging: decay, staleness, and soft forgetting

Forgetting is **retrieval-side first, storage-side second**: a stale memory should stop *surfacing* long before anything mutates rows.

**Retention model** (MemoryBank's Ebbinghaus curve, chosen for having exactly the two knobs we need):

```
R = exp( -t / S )
```

- `t` — time since `last_accessed_at`;
- `S` — `strength`, starts at 1.0, incremented on every reinforcement event (useful retrieval `+1`, re-observation `+2`, explicit confirm `+5`), which also resets `t`. Frequently-useful memories become effectively permanent; never-touched ones fade on a known curve.
- `R` multiplies into the retrieval score ([doc 04 §3](04-agent-interface.md)) — that's the soft forgetting.
- Kind-specific time constants: `episodic` decays ~4× faster than `semantic`; `procedural` ~4× slower; `invariant` is exempt (`R ≡ 1`).

**Storage-side sweeps** (consolidator, [doc 07 §2](07-operations.md)):

| Rule | Default |
|---|---|
| Staged, never retrieved, no reinforcement | archive after 30 days |
| Staged, contradicting an active memory, unresolved | escalate to review after 7 days, archive after 30 |
| Active, `R` below 0.05 (long-unaccessed) | archive after review batch; `PROPOSE`-style event first so it's visible |
| `expires_at` (policy TTL per category) reached | archive (or tombstone, if the category demands hard deletion — e.g. anything tagged `health`) |
| `deprecated` older than history-retention window | archive |

Archived is reversible (`RESTORE`); it is a working-set boundary, not a deletion.

## 6. Hard forgetting

Four hard paths, all detailed in [doc 06](06-audit-privacy-security.md):

1. **User forget** — "forget that about me" / review-UI delete: memory → `archived` (default) or `tombstoned` (on request); subject-scope memories always tombstone on explicit user request.
2. **Admin bulk revoke by provenance** — "everything derived from source S / learned by agent A / recorded in window T1–T2": reverse walk over `memory_derivations`, mass `QUARANTINE` then review → tombstone. This is the poisoning-incident lever.
3. **TTL tombstone** — categories whose policy mandates hard deletion at expiry.
4. **GDPR erasure** — `subject_ids` lookup + derivation cascade; hard-delete content and embeddings (including vector-index hygiene), keep content-free tombstone events.

## 7. Lifecycle of the worked scenario, compressed

```
2026-03-03  PROPOSE           staged   (agent_observed, scope channel/C0DEP, S=1.0)
2026-03-17  REINFORCE (re-obs) staged   S=3.0
2026-03-17  PROMOTE_STATUS    active
2026-04-02  POLICY_DECISION   ask      (scope promotion → workspace)
2026-04-02  CONFIRM (dana)    —
2026-04-02  PROMOTE_SCOPE     active   (scope workspace/T024B)
2026-05-14  READ (agent:marge, mr/482) S=4.0, t reset
2026-06-10  SUPERSEDE         deprecated (invalid_at=2026-06-10, superseded_by=<new>)
2027-06-10  ARCHIVE           archived  (history-retention sweep)
```

Continue with [doc 04 — Agent interface](04-agent-interface.md): the tools agents use to drive these transitions, and how retrieval actually works.
