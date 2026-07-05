# Memoramum — a memory system for AI agents

Memoramum is the design for a **standalone memory service** that lets heterogeneous AI agents — a Slack assistant, an MR-review agent, tomorrow's surfaces — **learn, remember, and forget**, under an explicit policy, with every memory **auditable** from the moment it was proposed to the moment it was erased.

This repository currently contains the architecture and design documentation. There is no implementation yet; the docs are written so that implementation can start from them directly.

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

## Glossary

The terms below are used consistently across all documents.

| Term | Meaning |
|---|---|
| **Memory** | An atomic, natural-language-first record an agent can recall. Has exactly one *kind*, one *scope*, one lifecycle *status*, and full provenance. |
| **Kind** | `semantic` (facts, preferences), `episodic` (what happened; exemplars), `procedural` (how to behave), `profile` (schema'd per-subject document, update-in-place). |
| **Scope** | The visibility container a memory lives in. Exactly one per memory. Scopes form a tree (org → surface → container → thread) plus parallel families: `subject` scopes (about a person, cross-surface) and `agent` scopes (agent-private). |
| **Scope chain** | The ordered list of scopes a read resolves against (e.g. channel → workspace → org, plus subject scopes of participants). |
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
| **Learning policy** | Declarative per-agent/surface config of what may be learned, where it routes, and the write decision mode (`allow / stage / ask / deny`). |
| **Sensitivity** | Classification on every memory: `public`, `internal`, `confidential`, `restricted`. Surfaces have sensitivity ceilings. |
| **Trust score** | Provenance-derived score used as a retrieval floor and poisoning defense. |

## Design stance in one paragraph

Memories are **superseded, never overwritten** (bi-temporal validity à la Zep/Graphiti); every memory keeps **PROV-shaped provenance** back to verbatim episodes; the write path is **policied** with three-valued decisions (`allow/stage/ask/deny`, Claude-Code-style layering with an admin floor); the read path enforces access **at retrieval time** (Slack's invariant: never surface what the requesting principal couldn't see at the source); forgetting is **soft first** (decay out of retrieval) and **hard when required** (TTL, quarantine, GDPR erasure with tombstones); and the reference implementation is **one boring Postgres** (+pgvector) so that memories, events, scopes, and policy live in a single transactional store.
