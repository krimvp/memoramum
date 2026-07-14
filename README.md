# Memoramum — a memory system for AI agents

Memoramum is the design for a **standalone memory service** that lets heterogeneous AI agents — a Slack assistant, an MR-review agent, tomorrow's surfaces — **learn, remember, and forget**, under an explicit policy, with every memory **auditable** from the moment it was proposed to the moment it was erased.

This repository contains the architecture and design documentation, and a **reference implementation** that follows the design's phased rollout ([doc 07 §6](docs/07-operations.md)) — currently at **P5** (dev-time & module scopes), the final phase. The docs are normative; the code implements them.

## Why this exists

Agents without memory re-ask the same questions, re-learn the same lessons, and repeat the same mistakes every session. Agents with *naive* memory are worse: they hoard stale facts, leak information across contexts that should be isolated, can be poisoned by anything they read, and nobody can explain why the bot "knows" something.

The industry state of the art (surveyed in [the prior-art appendix](docs/appendix-prior-art.md)) solves pieces of this — Letta's self-editing memory, Mem0's extraction pipeline, Zep's temporal knowledge graph, LangMem's memory taxonomy — but none of them treats **provenance, scoped access control, and governance** as first-class. Memoramum's bet is that for a company platform, those are not features to bolt on later; they are the core of the design:

1. **Auditable by construction.** Every memory knows where it came from (the exact source messages), who caused it (user, agent-on-behalf-of-user, system), why it was kept (an explicit justification), and whether it was an explicit user ask or an LLM's suggestion. Every write *and every read into an agent's context* is an event in an append-only log.
2. **Scoped by default, shared by exception.** A memory learned in a Slack channel belongs to that channel. Making it visible workspace-wide or to a different surface (like the MR-review agent) is an explicit, policied, audited **promotion** — never a side effect.
3. **Governed by policy, not by vibes.** A separate policy layer decides what each agent may learn, where it may write, what it may read, and what requires human confirmation. An agent with no learning policy attached learns nothing.
4. **Forgetting is a feature.** Memories have a lifecycle — `staged → active → invariant / deprecated → archived → tombstoned` — with reinforcement-based promotion, decay-based retirement, contradiction-driven supersession, and GDPR-grade erasure.

## The worked scenario

One scenario threads through every document, so each mechanism can be seen end-to-end:

> 1. **Learn.** The Slack agent *Sage*, active in the `#deploys` channel, observes @dana say "reminder: Team Atlas deploys to production only on Tuesdays." A background extraction pass proposes a memory. Policy allows it → it lands as **staged**, scoped to that channel, with provenance pointing at dana's exact message.
> 2. **Reinforce.** Two weeks later the fact is re-observed and retrieved usefully in a thread. The consolidator promotes it to **active**.
> 3. **Promote.** @dana asks Sage to "make sure the other teams know this too." Promotion from channel scope to workspace scope is a shared-scope crossing → policy requires user confirmation → dana confirms → an audited promotion event.
> 4. **Cross-surface recall.** The MR-review agent *Marge*, reviewing a Friday MR that triggers a production deploy, resolves its scope chain (repo → org + relevant shared scopes it is enrolled in), retrieves the memory, and flags "Team Atlas deploys only on Tuesdays — this MR schedules a Friday deploy."
> 5. **Forget.** Months later someone posts "we've moved to daily deploys." Contradiction detection closes the old memory's validity window (`invalid_at`), creates the successor, links the two. The old memory stops surfacing but remains queryable for history.
> 6. **Audit.** A platform admin asks "why did Marge warn about Tuesday deploys on MR !482?" — the read event, the memory, its promotion, its reinforcements, and dana's original Slack message are one traversal away.

## Document map

| Doc | Contents |
|---|---|
| [01 — Concepts & scopes](docs/01-concepts-and-scopes.md) | Core nouns: memories, kinds, scopes, principals; the scope tree and scope chains |
| [02 — Data model](docs/02-data-model.md) | Memory record, provenance, episodes, append-only event log; reference SQL DDL |
| [03 — Lifecycle](docs/03-lifecycle.md) | Tier state machine, bi-temporal validity, reinforcement, decay, forgetting |
| [04 — Agent interface](docs/04-agent-interface.md) | MCP tools & REST API; ambient vs deliberate recall; retrieval scoring; agent prompt contract |
| [05 — Policy](docs/05-policy.md) | Learning policy, access policy (ReBAC + attribute rules), enforcement points |
| [06 — Audit, privacy & security](docs/06-audit-privacy-security.md) | Audit surfaces, GDPR erasure, memory-poisoning defenses, PII pipeline |
| [07 — Operations](docs/07-operations.md) | The consolidator, reference stack, observability, phased rollout |
| [Appendix — Prior art](docs/appendix-prior-art.md) | Survey of Letta, Mem0, Zep/Graphiti, LangMem, product memory systems, academic work — and what we took from each |
| [ADRs](docs/adr/) | One-page records for the contested decisions |

## The reference implementation

`src/memoramum/` implements the design phase by phase (stack decision: [ADR-0007](docs/adr/0007-python-reference-implementation.md); Python + one Postgres 16/pgvector, FastAPI REST facade, MCP facade). **P1** is implemented: the four core tables exactly as specified in [doc 02](docs/02-data-model.md), episodes, `explicit_user_ask` writes, `memory_remember` / `memory_recall` / `memory_status` over MCP, the context block and platform endpoints of [doc 04](docs/04-agent-interface.md), the scope tree + enrollment with retrieval-time access checks, and the full event log including `READ`s (with the subject-scope hash chain). **P2** is implemented: the staged tier filled by `llm_inferred` hot-path writes, `memory_reinforce` plus write-time re-observation, the staged→active promotion rule of [doc 03 §4](docs/03-lifecycle.md), supersession and held-contradiction handling (staged input never supersedes active memories; contradiction judging is a pluggable seam with deterministic dev judges, like the embedder), the consolidator's P2 jobs — status promotion, contradiction-review timers, the decay/TTL sweeps — and the staged-triage review endpoints. **P3** is implemented: the full learning-policy engine of [doc 05](docs/05-policy.md) — versioned policy documents (YAML in, canonical JSON out) evaluated org → surface → agent → user-preference with strictest-wins composition, policy-decided routing, and the user-preference layer — `ask` as a first-class two-step (pending store, `memory_confirm`, confirmations recorded as the human's events), scope promotion with the [doc 05 §3](docs/05-policy.md) gates and confirmers (`memory_promote`, worked-scenario steps 3–4), the PII pipeline of [doc 06 §4](docs/06-audit-privacy-security.md) in the classify step (block/tokenize/redact behind a pluggable analyzer seam, embeddings computed after redaction), read-side sensitivity ceilings, trust floors and category deny-lists, per-category retention overrides (TTL + tombstone-at-expiry), and policy administration with simulation mode. **P4** is implemented: extraction workers ([doc 07 §1](docs/07-operations.md) — debounced per-scope batches behind a pluggable extractor seam, proposing `agent_observed` candidates through the same write pipeline as any agent), the full consolidator job set of [doc 07 §2](docs/07-operations.md) (dedupe/merge into consolidated successors with weakest-input tier and minimum-input trust, reflection behind a `Reflector` seam whose candidates go through the write pipeline with procedural outputs defaulting to `ask`, and the hygiene sweep — reclassification after classifier upgrades, the [doc 06 §3](docs/06-audit-privacy-security.md) poisoning anomaly checks, ghost-vector repair), provenance-derived trust at write time, `memory_forget` with the narrow-agent-powers/`ask` gate of [doc 04 §1](docs/04-agent-interface.md), lineage quarantine with review (source predicate → reverse derivation walk → mass `QUARANTINE` → restore or tombstone) plus the break-glass per-agent write-freeze, and the hardened GDPR erasure pipeline of [doc 06 §2](docs/06-audit-privacy-security.md) — derived-closure enumeration, consolidated-derivative regeneration from surviving sources, physical content/embedding deletion, content-free tombstone events, and HMAC-signed attestations. The [doc 07 §6](docs/07-operations.md) P4 exit criterion — the full worked scenario, end-to-end across Slack + GitLab — runs as `tests/test_worked_scenario.py`. **P5** is implemented: the `module` scope family and the admin-maintained `module_paths` path-glob mapping ([ADR-0009](docs/adr/0009-module-scope-family.md), [ADR-0010](docs/adr/0010-module-boundary-detection.md)), with module scopes pulled into a read's scope chain by the paths a flow touches; the `mr → module` and stricter `module → project` promotion crossings with their `module_owner` / `maintainer` confirmer tuples ([ADR-0011](docs/adr/0011-module-promotion-crossings.md)); the personal dev-time surface `surface:ide` with the seventh MCP tool `memory_observe` registering client-side `dev_observation` episodes at a lower trust base ([ADR-0012](docs/adr/0012-dev-time-agent-surface.md)); and the codebase-convention routing defaults that land dev-time conventions in the touched module scope while personal task tactics stay agent-private ([ADR-0013](docs/adr/0013-dev-time-routing-defaults.md)) — the [doc 07 §6](docs/07-operations.md) P5 exit criterion, the module-scope scenario, runs as `tests/test_module_scenario.py`. Beyond the phases, the observability surface of [doc 07 §4](docs/07-operations.md) is implemented too: the "metrics that matter" — useful-read ratio, staged-tier precision, contradiction-queue depth, decisions by verdict per agent, tier populations, erasure-SLO timing with attestation coverage, READ-event batching — are computed from the store rather than from process counters (the event log already *is* the instrumentation, [ADR-0008](docs/adr/0008-metrics-from-the-store.md)) and served as one snapshot at `GET /v1/metrics`. The REST facade authenticates callers with principal-bound bearer tokens ([ADR-0014](docs/adr/0014-bearer-token-rest-auth.md)) — the token, not a caller-asserted header, names the `actor` of every call — falling back to a loopback-only dev-mode shim when no tokens are configured. The MCP facade — the only agent-facing surface — serves the same seven-tool registry over two transports ([ADR-0015](docs/adr/0015-remote-mcp-streamable-http.md)): per-session stdio, spawned by the surface integration with the principal pair and flow in its environment, and streamable HTTP, served centrally so harnesses connect to the deployed service with the same principal-bound bearer tokens, asserting `on_behalf_of` and the flow in per-request `X-Memoramum-*` headers. The [doc 07 §5](docs/07-operations.md) failure modes are implemented as specified: a per-surface membership-sync watermark with the 5-minute staleness bound (beyond it, private surface scopes fail closed and other reads note the staleness in their `READ` event), an unreachable policy engine failing writes closed (`503`, queue and retry) and restricting reads to the requesting agent's own `agent:*` scope, and MCP tools returning explicit unavailability — "I can't check my memory right now" — instead of guessing.

```bash
make venv        # python -m venv + pip install -e '.[dev]'
make db          # docker compose up postgres (16 + pgvector)
make migrate     # apply src/memoramum/migrations/ (the doc 02 DDL, verbatim)
make test        # integration tests incl. the P1–P5 exit criteria of doc 07 §6
make api         # REST facade on :8385; memoramum-mcp is the agent facade
make consolidate # one consolidator run (the doc 07 §2 job set); nightly in production
make extract     # one extraction-worker pass (doc 07 §1); scheduled in production
```

## Glossary

The terms below are used consistently across all documents.

| Term | Meaning |
|---|---|
| **Memory** | An atomic, natural-language-first record an agent can recall. Has exactly one *kind*, one *scope*, one lifecycle *status*, and full provenance. |
| **Kind** | `semantic` (facts, preferences), `episodic` (what happened; exemplars), `procedural` (how to behave), `profile` (schema'd per-subject document, update-in-place). |
| **Scope** | The visibility container a memory lives in. Exactly one per memory. Scopes form a tree (org → surface → container → thread) plus parallel families: `subject` scopes (about a person, cross-surface), `agent` scopes (agent-private), and `module` scopes (monorepo-module conventions). |
| **Scope chain** | The ordered list of scopes a read resolves against (e.g. channel → workspace → org, plus subject scopes of participants). |
| **Module scope** | `module:*` — a parallel scope family under a project container for monorepo-module conventions; pulled into scope chains by the paths a flow touches. |
| **Promotion** | Explicit, policied, audited move of a memory to a broader scope. |
| **Principal** | An identity that acts: `user`, `agent` (optionally on-behalf-of a user), or `system` (e.g. the consolidator). |
| **Origin kind** | How a memory came to be: `explicit_user_ask`, `llm_inferred`, `agent_observed`, `consolidated`, `imported`. |
| **Episode** | A verbatim raw source (a Slack message ref, an MR comment ref) — the provenance atom memories derive from. |
| **Status** | Lifecycle tier: `staged`, `active`, `invariant`, `deprecated`, `archived`, `tombstoned`. |
| **Staged** | Usable-but-untrusted quarantine tier for agent-inferred memories; promotes via reinforcement. |
| **Invariant** | Pinned memory that never decays and can only be changed by an explicit privileged action. |
| **Deprecated** | Superseded or invalidated; excluded from current-state recall, retained for history. |
| **Tombstone** | Content-free marker left in the audit trail after hard erasure. |
| **Reinforcement** | Signal that a memory is correct/useful (re-observation, useful retrieval, explicit confirmation); drives staged→active promotion and resets decay. |
| **Consolidator** | The background system actor that dedupes, detects contradictions, promotes, decays, summarizes, and expires memories. |
| **Ambient recall** | Service-assembled context block for a scope chain, injected at session/turn start. |
| **Deliberate recall** | Mid-task, agent-initiated memory search tool call. |
| **Harness** | The runtime that hosts an agent and its MCP client (a surface integration, an IDE, an agent framework). It launches or connects to the MCP facade, holds the agent's bearer token, and asserts the flow — agents themselves never hold credentials. |
| **Learning policy** | Declarative per-agent/surface config of what may be learned, where it routes, and the write decision mode (`allow / stage / ask / deny`). |
| **Sensitivity** | Classification on every memory: `public`, `internal`, `confidential`, `restricted`. Surfaces have sensitivity ceilings. |
| **Trust score** | Provenance-derived score used as a retrieval floor and poisoning defense. |

## Design stance in one paragraph

Memories are **superseded, never overwritten** (bi-temporal validity à la Zep/Graphiti); every memory keeps **PROV-shaped provenance** back to verbatim episodes; the write path is **policied** with three-valued decisions (`allow/stage/ask/deny`, Claude-Code-style layering with an admin floor); the read path enforces access **at retrieval time** (Slack's invariant: never surface what the requesting principal couldn't see at the source); forgetting is **soft first** (decay out of retrieval) and **hard when required** (TTL, quarantine, GDPR erasure with tombstones); and the reference implementation is **one boring Postgres** (+pgvector) so that memories, events, scopes, and policy live in a single transactional store.
