# 07 — Operations

The runtime shape of the service: the background machinery ("memory itself should have some maintenance"), the reference stack, observability, and a phased rollout.

---

## 1. Components

```
                       ┌────────────────────────────────────────────┐
  Slack events ──▶     │                MEMORAMUM                   │
  GitLab webhooks ──▶  │                                            │
                       │  Ingestion ──▶ episodes                    │
   agents (MCP) ◀──────┤  MCP facade ─┐                             │
   platform (REST) ◀───┤  REST facade ┼─▶ API core ──▶ Postgres     │
                       │              │     │   (memories, episodes,│
   review UI ◀─────────┤              │     │    events, scopes,    │
                       │       policy engine    policy)             │
                       │              │                             │
                       │  job queue ──▶ extraction workers          │
                       │            ──▶ consolidator                │
                       │            ──▶ sweeps (decay/TTL/erasure)  │
                       └────────────────────────────────────────────┘
```

- **Ingestion** subscribes to surface event streams and registers episodes (refs always, verbatim by policy). It also feeds the **membership sync** used by retrieval-time access checks ([doc 05 §4](05-policy.md)).
- **Dev-time registration** — the personal `surface:ide` ([ADR-0012](adr/0012-dev-time-agent-surface.md)) has no platform-side subscriber; its MCP client pushes episodes directly via `memory_observe` ([doc 04 §1](04-agent-interface.md)) rather than through webhook-subscribed ingestion. Same write pipeline, lower `dev_observation` trust base.
- **API core** is the single enforcement point: both facades route through it; nothing reaches Postgres except through it (plus RLS as defense-in-depth).
- **Extraction workers** implement `agent_observed` learning: debounced background reflection over recent episodes per scope (the LangMem `ReflectionExecutor` pattern — accumulate, cancel-and-reschedule on new activity, run at conversation-lull; default debounce 30 min, cap 4 h). Extraction proposes candidates through the same write pipeline as any agent — policy applies identically.
- **Consolidator** — §2.
- **Sweeps** — scheduled jobs for decay scoring, TTL/archival, erasure verification.

## 2. The consolidator

The system's "sleep-time" worker (Letta's sleep-time-agent insight, run as a service job rather than a second agent): memory quality is produced *offline*, so the hot path stays fast and dumb. Runs per-scope on a schedule (default nightly) plus event-triggered micro-runs (contradiction queued, staged threshold reached). It is `system:consolidator` — every action it takes is evented like any principal, and its outputs carry `origin_kind=consolidated` provenance naming the rule/prompt version that produced them.

| Job | What it does |
|---|---|
| **Dedupe/merge** | Embedding-neighborhood + lexical candidates within a scope → LLM judge → merge (successor memory `derived_from` both; predecessors deprecated). Mem0's ADD/UPDATE/NOOP decision, moved off the write path. |
| **Contradiction review** | Works the held-contradiction queue ([doc 03 §3](03-lifecycle.md)): weigh evidence, supersede / keep both with validity windows / escalate to human review. |
| **Status promotion** | Applies the reinforcement rules ([doc 03 §4](03-lifecycle.md)) — staged→active transitions batch here. |
| **Reflection/summarization** | Clusters of related `episodic` memories → distilled `semantic`/`procedural` candidates (Generative-Agents reflection). Candidates go through the write pipeline; `procedural` outputs default to `ask`. |
| **Decay & sweeps** | Recompute `R`, archive per the rules table in [doc 03 §5](03-lifecycle.md), enforce TTLs (tombstoning where policy demands). |
| **Hygiene** | Reclassification after classifier upgrades ([doc 06 §4](06-audit-privacy-security.md)), poisoning anomaly checks ([doc 06 §3](06-audit-privacy-security.md)), orphan/ghost-vector verification after erasures. |

Budgeting: consolidator LLM spend is per-scope-capped and prioritized by scope activity — busy scopes get nightly attention, dormant ones weekly. All jobs are idempotent and resumable; a wedged consolidator degrades quality, never correctness (the write path does not depend on it).

## 3. Reference stack

Decision recorded in [ADR-0004](adr/0004-postgres-reference-stack.md); summary:

| Concern | Choice | Why |
|---|---|---|
| Primary store | **Postgres 16+ with pgvector** | Memories, episodes, events, scopes, policy in *one transactional store*: a write + its provenance + its event commit atomically. HNSW + tsvector give hybrid retrieval natively. RLS as defense-in-depth under the API core. Single-org scale (≤ low millions of memories, ≤ thousands of QPS reads) is comfortably inside Postgres territory. |
| Queue | any boring queue (SQS / Postgres-based) | Debounce and sweeps need at-least-once + delay, nothing exotic. |
| Policy engine | in-service evaluation over policy tables | ReBAC tables kept SpiceDB/OpenFGA-isomorphic; OPA sidecar as documented escape hatch ([doc 05 §2](05-policy.md), §4). |
| Facades | MCP server + REST | [ADR-0005](adr/0005-standalone-service-mcp.md). |

**When to revisit** (the triggers table, so this doesn't ossify):

| Trigger | Move |
|---|---|
| Vector corpus or QPS outgrows pgvector (sustained p95 recall > ~150 ms at target K) | dedicated vector store (Qdrant) with `scope_id` payload filtering *inside* traversal; Postgres stays system-of-record |
| Multi-hop questions become common ("who owns the service dana's team deploys on Tuesdays?") and flat-fact retrieval visibly fails them | temporal knowledge graph layer (Graphiti-style entity/edge extraction) *on top of* episodes — the episode+provenance design was chosen to make this additive, not a migration |
| Multi-tenancy | tenant becomes a physical boundary (schema- or cluster-per-tenant), ReBAC moves to a real engine |
| Policy conditions outgrow the declarative shape | promote the OPA sidecar from escape hatch to standard path |
| Store-derived metrics snapshots ([ADR-0008](adr/0008-metrics-from-the-store.md)) get slow at event volume, or trend/alerting needs outgrow point-in-time reads | materialize the aggregates (rollup tables refreshed by a sweep), or put a time-series exporter in front of `GET /v1/metrics`; the endpoint shape stays |

## 4. Observability & SLOs

**Metrics that matter** (beyond standard service health) — computed from the store rather than from process counters, and served as one snapshot at `GET /v1/metrics` ([doc 04 §5](04-agent-interface.md); decision recorded in [ADR-0008](adr/0008-metrics-from-the-store.md)):

- *Retrieval quality*: recall-into-context rate that gets reinforced (useful-read ratio); staged-tier precision (fraction of staged memories eventually promoted vs archived — the health of the extraction pipeline); contradiction-queue depth and age.
- *Policy*: decisions by verdict per agent (a spike in `deny` = misconfigured agent or an attack; a spike in `ask` = user-fatigue risk); time-to-confirm for `ask`s.
- *Lifecycle*: tier population by scope family; decay-archive volume; median staged→active time.
- *Audit/compliance*: event-log lag, erasure-pipeline completion time (SLO: enumerate < 1 h, complete < 72 h), attestation coverage.
- *Cost*: consolidator tokens per scope; embedding volume; READ-event write amplification ([ADR-0006](adr/0006-log-reads.md) mitigations: batch per delivery, partition by month).

**SLO sketch**: context-block assembly p95 < 300 ms; `memory_recall` p95 < 500 ms; write-pipeline (with classification) p95 < 2 s (staging is async-tolerant); event durability = transactional with the write (no fire-and-forget audit).

## 5. Failure modes

| Failure | Behavior |
|---|---|
| Memory service down | Agents degrade to memoryless operation — surfaces must treat the context block as optional enrichment. MCP tools return explicit unavailability; the prompt contract tells agents to say "I can't check my memory right now," not to guess. |
| Policy engine unreachable | **Fail closed** for writes (queue and retry), fail closed for reads beyond the requesting principal's own `agent:*` scope. |
| Membership sync stale | Retrieval uses last-synced membership with a staleness bound (default 5 min); beyond the bound, private-trust-class scopes fail closed, others serve with staleness noted in the event. |
| Classifier/extraction model regression | Versions recorded in provenance; reclassification sweep + quarantine-by-activity available ([doc 06](06-audit-privacy-security.md)). |

## 6. Phased rollout

Each phase is shippable and useful on its own; nothing later requires reworking earlier data (the schema carries all fields from day one; later phases *populate* them). The reference implementation in `src/` lands strictly by these phases ([ADR-0007](adr/0007-python-reference-implementation.md)).

| Phase | Scope | Exit criteria |
|---|---|---|
| **P1 — Remember & recall, audited** | Core store; episodes; `explicit_user_ask` writes only (`memory_remember`/`memory_recall`/`memory_status` + context block); scope tree + enrollment; full event log incl. READs; per-memory history + per-subject views. One surface (Slack), one agent. | Worked-scenario steps 1 (explicit variant), 6 demonstrable end-to-end. |
| **P2 — Lifecycle** | Staged tier + `llm_inferred` hot-path writes; reinforcement + decay + sweeps; supersession & contradiction handling; review UI for staged triage. | Staged→active promotions happening organically; contradiction demo (scenario step 5). |
| **P3 — Policy & promotion** | Full learning-policy engine (layers, strategies, `ask` flows); scope promotion with confirmations; PII pipeline; sensitivity ceilings & trust floors; user preference layer. | Scenario steps 3–4 across two scopes; policy simulation mode working. |
| **P4 — Background learning & cross-surface** | Extraction workers (`agent_observed`); consolidator full job set; second surface (MR-review agent) + subject scopes in anger; quarantine tooling; erasure pipeline hardened (attestation). | The full worked scenario, verbatim, across Slack + GitLab. |
| **P5 — Dev-time & module scopes** | `module` scope family + path-glob boundary config (`module_paths`); `mr → module` / `module → project` promotions; personal dev-time surface (`surface:ide`) with client-registered episodes (`memory_observe`); dev-time routing defaults. | The module-scope scenario: dev-time observation → shared scope; MR candidates across modules; `mr → module` promotion via `module_owner`; sibling-module contradiction isolation; a later MR surfacing only its own module's convention — runs as `tests/test_module_scenario.py`. |

Deliberately **not** in scope until a trigger fires: knowledge-graph retrieval, multi-tenancy, cross-org sharing, agent-to-agent memory exchange outside shared scopes.

---

*End of the core design. See the [prior-art appendix](appendix-prior-art.md) for where these ideas were stolen from, and [docs/adr/](adr/) for the contested decisions.*
