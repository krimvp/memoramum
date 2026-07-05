# Appendix — Prior art

Survey of the systems and research that informed this design (as of mid-2026), with the specific lesson each contributed — adopted or deliberately rejected. Deep-dive citations at the end.

---

## 1. Letta (formerly MemGPT)

**Mechanism.** OS-inspired memory hierarchy: *core memory* — small, always-in-context, self-editable blocks (`persona`, `human`, custom; ~2k chars each) edited by the agent through tools (`memory_insert/replace/rethink`); *recall memory* — searchable conversation history; *archival memory* — vector store the agent explicitly inserts into and searches. The MemGPT paper (arXiv:2310.08560) adds the machinery: memory-pressure warnings at ~70% context fill, FIFO eviction with recursive summarization at 100%. Newer Letta adds **sleep-time agents**: a background agent sharing memory blocks with the primary one, exclusively responsible for rewriting them (`rethink_memory` loops) — memory formation moved off the hot path, primary agent kept fast. Blocks are shareable across agents via a join table.

**Adopted:** memory-editing as explicit tool calls with a small verb set (our doc 04); the sleep-time principle — quality work happens offline (our consolidator, doc 07 §2); block `description` fields teaching the agent *how* to use each memory (our prompt contract).
**Rejected:** agent-editable in-place memory as the primary store — the edit destroys history and provenance ("no first-class undo; reconstruction by the same LLM that may be the corruption source"). Our content is immutable; state changes are events.

## 2. Mem0

**Mechanism** (arXiv:2504.19413). Two-phase pipeline: LLM *extraction* of candidate facts from recent messages + rolling summary, then an *update* phase where the LLM compares each candidate against top-10 similar existing memories and emits `ADD / UPDATE / DELETE / NOOP`. Scoping by `user_id/agent_id/run_id/app_id`. A SQLite `history` table records old/new value, event, actor per memory — the best per-memory changelog API in the field. Platform adds `expiration_date`, custom categories, criteria-based reranking. (Note: 2026-era Mem0 v3 moved to ADD-only extraction with hybrid vector+BM25+entity-boost search, dropping the graph variant from OSS; the vendor's benchmark claims in both directions of the Mem0-vs-Zep dispute are contested — we treat all LOCOMO numbers as marketing.)

**Adopted:** the per-memory history API shape (`GET /memories/{id}/history`, doc 06 §1.2); extraction as a distinct, promptable pipeline stage; categories as a policy vocabulary.
**Rejected:** LLM-decided in-place `UPDATE`/`DELETE` on the write path — it makes an LLM judgment destroy data with no quarantine. Our equivalent decisions live in the consolidator, produce successors, and cannot touch higher-trust tiers ([ADR-0001](adr/0001-supersede-dont-overwrite.md)).

## 3. Zep / Graphiti

**Mechanism** (arXiv:2501.13956). Temporal knowledge graph in three tiers: *episodes* (verbatim raw inputs), *entities/edges* (facts extracted and deduped by LLM judges, each edge carrying the episode ids that support it), *communities* (cluster summaries). **Bi-temporal**: `valid_at/invalid_at` (true in the world) vs `created_at/expired_at` (known to the system). Contradictions **invalidate, never delete** — the old edge's window closes, history stays queryable. Retrieval is hybrid (cosine + BM25 + graph traversal) fused with RRF; product layer assembles a "context block" for direct prompt injection.

**Adopted (the most-borrowed system here):** episodes as provenance atoms; bi-temporal validity fields verbatim; invalidate-don't-delete; the assembled context block; hybrid retrieval with RRF. 
**Rejected (for now):** the knowledge graph itself — entity resolution and graph maintenance are heavy machinery we don't need for flat scoped facts; our episode+derivation design deliberately keeps a Graphiti-style layer *additive* later (doc 07 §3 triggers). Also absent in Zep: per-fact access control and any real policy layer — the gap this design exists to fill.

## 4. LangMem / LangGraph memory

**Mechanism.** The cleanest taxonomy: **semantic / episodic / procedural**, with semantic split into *profile* (single schema'd doc, update-in-place) vs *collection* (unbounded records). Storage is a namespaced JSON `BaseStore` (tuple namespaces, TTL support, semantic search). Two write paths named explicitly: **hot path** (agent tool call, "conscious") vs **background** (`ReflectionExecutor` — debounced reflection that cancels/reschedules on new activity, "subconscious"). Procedural memory implemented as prompt optimization over trajectories.

**Adopted:** the kind taxonomy including the profile exception (doc 01 §2); hot-path/background duality (docs 04/07); the debounce pattern for extraction workers.
**Rejected:** namespace-discipline-as-access-control — LangMem's own docs state there is no access control, provenance, or governance; conventions are not enforcement. Every gap in that list is a first-class subsystem here.

## 5. Product memory systems (ChatGPT, Claude)

**ChatGPT**: two tiers — user-visible *saved memories* (bio tool; injected each session) and *inferred chat-history insights* ("dreaming": background synthesis of preferences/highlights). Controls: per-tier toggles, per-memory delete, temporary chats, workspace-admin kill switch, project-only memory isolation. The inferred tier is **not inspectable per-item** — the most instructive negative example in the field.
**Claude.ai**: rolling per-scope memory *summary* the user can read and directly edit; project-scoped isolation; incognito; org-admin disable. **Claude Code**: layered CLAUDE.md (enterprise floor > user > project > local) + model-authored auto-memory directory; explicit note that memory is *persuasive context*, while enforcement belongs to permissions/hooks. **Claude API memory tool**: client-owned file-based memory — storage, scoping, and security are entirely the integrator's problem (i.e., a system like this one).

**Adopted:** explicit-vs-inferred as distinct origin kinds with different lifecycles; incognito/no-memory flows; admin kill switches; the enterprise-floor layering (our org policy layer); "memory is context, policy is enforcement."
**Rejected:** non-inspectable inferred memory (our staged tier is exactly as auditable as everything else); memory summaries as the *storage* format (a summary is a view; we store atomic facts and can render summaries).

## 6. Academic work

- **Generative Agents** (Park et al., arXiv:2304.03442): append-only memory stream scored at retrieval by `recency · importance · relevance` (exponential recency decay 0.995/hour on *last retrieval*; LLM-rated importance at write; cosine relevance) and **reflection** — when accumulated importance crosses a threshold, synthesize higher-level insights citing their evidence memories. → Our composite retrieval score (doc 04 §3) and the consolidator's reflection job; "citing evidence" is our `derived_from`.
- **MemoryBank** (arXiv:2305.10250): Ebbinghaus retention `R = e^(−t/S)`, strength `S` incremented and `t` reset on recall — the spacing effect. → Adopted verbatim as the decay model (doc 03 §5), chosen over Generative-Agents-style pure recency because reinforcement is exactly the staged-promotion signal we need anyway.
- **HippoRAG** (arXiv:2405.14831): KG + Personalized PageRank for multi-hop retrieval; node-specificity weighting. → Not adopted; informs the "when flat retrieval fails multi-hop" trigger (doc 07 §3).
- **A-MEM** (arXiv:2502.12110): Zettelkasten notes whose arrival can rewrite older notes' metadata ("memory evolution"), schema-free by conviction. → Rejected as a write model (unconstrained LLM rewrites of old memories = provenance destruction), but its link-generation informs the consolidator's dedupe candidate search.
- **Reflexion / Voyager**: verbal self-critique buffers; skill libraries added only after self-verification. → The `episodic → procedural` distillation path, and "verify before promoting" as a lifecycle principle.
- **Memory-mechanism survey** (arXiv:2404.13501): the writing/management/reading operation split, with management = reflection + merging + forgetting — a sanity check that doc 07 §2's job list covers the space.

## 7. Governance & security prior art

- **W3C PROV** (entity/activity/agent, `wasDerivedFrom`): the shape of `memory_provenance` + `memory_derivations`. PROV-AGENT extends it to agent workflows.
- **Google Zanzibar / SpiceDB / OpenFGA**: relation-tuple ReBAC; our scope-membership tables are kept isomorphic for a future migration ([ADR-0004](adr/0004-postgres-reference-stack.md)). OPA/Rego as the ABAC escape hatch.
- **Slack AI's security contract**: "never surface what the requester couldn't see at the source, checked at request time" — adopted as the source-visibility invariant (doc 01 §3.2), arguably this design's most important single rule.
- **AWS Bedrock AgentCore memory strategies**: learning as declarative, attached configuration (no strategy → nothing learned) with namespace routing and per-strategy retention — the template for our learning policy (doc 05 §1).
- **Claude Code permissions**: layered allow/ask/deny with an enterprise floor and pre-action hooks — the template for verdicts and layering (doc 05 §2).
- **OWASP Agentic Top 10 (ASI06, memory & context poisoning)** and the agent-memory-safety literature: memory writes are an attack surface; provenance-based trust scoring, quarantine tiers, and lineage revocation as the defenses (doc 06 §3).
- **GDPR erasure practice**: tombstones vs hard delete, the ghost-vector problem (soft-deleted HNSW embeddings remain reconstructible), crypto-shredding, and the derived-artifact cascade — all reflected in the erasure pipeline (doc 06 §2).
- **Microsoft Presidio**: analyzer/anonymizer pipeline with reversible tokenization — the PII pipeline's reference implementation (doc 06 §4).

## 8. The gap this design fills

Lined up against the requirement list, every surveyed system is strong on one axis and silent on the others:

| | Letta | Mem0 | Zep | LangMem | ChatGPT/Claude | **Memoramum** |
|---|---|---|---|---|---|---|
| Agent-driven learn/forget | ●● | ●● | ● | ●● | ● | ●● |
| Provenance to source | ○ | ● | ●● | ○ | ○ | ●● |
| Audit (incl. reads, decisions) | ○ | ● | ○ | ○ | ○ | ●● |
| Scoped access control | ○ | ● (ids) | ● (partitions) | ○ | ● (product-level) | ●● |
| Lifecycle tiers / forgetting | ● | ● | ● (invalidation) | ● (TTL) | ● (user delete) | ●● |
| Policy / governance layer | ○ | ○ | ○ | ○ | ● (admin toggles) | ●● |

(●● first-class, ● partial, ○ absent.) The synthesis — Zep's temporal provenance + LangMem's taxonomy + Letta's offline consolidation + Bedrock's declarative learning policy + Slack's retrieval-time access contract + a compliance-grade audit log — is the design in docs 01–07.

---

### Source notes

Primary sources consulted: arXiv:2310.08560 (MemGPT), 2504.19413 (Mem0), 2501.13956 (Zep), 2304.03442 (Generative Agents), 2305.10250 (MemoryBank), 2405.14831 (HippoRAG), 2502.12110 (A-MEM), 2303.11366 (Reflexion), 2305.16291 (Voyager), 2404.13501 (memory survey); docs.letta.com, docs.mem0.ai, help.getzep.com, langchain-ai.github.io/langmem; getzep/graphiti and mem0ai/mem0 source code; W3C PROV-DM; OWASP Agentic AI Top 10 / Agent Memory Guard; AWS Bedrock AgentCore memory docs; Slack AI security documentation; OpenAI memory FAQ + "dreaming" post; Anthropic memory-tool and Claude Code memory docs. Vendor benchmark claims (LOCOMO/LongMemEval, in both directions of the Mem0↔Zep dispute) are contested and were not relied on for any design decision.
