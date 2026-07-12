# 04 — Agent interface

How agents actually use the system: the tool surface, the two read paths, the write paths, retrieval scoring, and the prompt contract that teaches agents *when* to remember and recall. This answers "agents should know how and when to access this memory."

The service exposes two facades over one API core ([ADR-0005](adr/0005-standalone-service-mcp.md)):

- an **MCP server** — the primary agent-facing surface; tools below are MCP tool definitions;
- a **REST/gRPC API** — for platform code (session bootstrapping, review UIs, admin, audit queries).

Every call is authenticated as a principal pair (`agent`, optional `on_behalf_of` user) and carries a **flow context** — surface, container, session id — from which the service resolves the scope chain. For dev-time flows (`surface:ide`) the context also names the project it works against and the paths it touched, which the service maps onto the *existing* `project/*` and `module:*` scopes ([ADR-0012](adr/0012-dev-time-agent-surface.md)). Agents never name raw scope ids in reads; they describe where they are, the service decides what that makes visible.

---

## 1. Tool surface (MCP)

Seven tools. Deliberately few: every agent-facing memory system that works (Letta, MemGPT, Claude's memory tool) keeps the verb set small and pushes intelligence into descriptions and prompts.

### `memory_recall`

```jsonc
{ "query": "deploy schedule for team atlas",     // natural language; required
  "kinds": ["semantic","procedural"],            // optional filter
  "subjects": ["user:dana"],                     // optional: memories ABOUT these principals
  "include_staged": true,                        // default true; policy may force false
  "as_of": null,                                 // ISO time → historical belief query
  "limit": 8 }
```

Returns scored memories, each with: `id`, `content`, `kind`, `status`, `scope_id`, `valid_at`/`invalid_at`, `confidence`, and a one-line provenance hint (`"observed in #deploys, 2026-03-03, from @dana; reinforced 2×"`). The provenance hint is not decoration — agents are instructed to weigh and, where useful, *cite* it ("per @dana in #deploys…"), which is what makes agent answers auditable by users downstream.

### `memory_remember`

```jsonc
{ "content": "Team Atlas deploys to production only on Tuesdays",
  "kind": "semantic",
  "origin_kind": "llm_inferred",             // or explicit_user_ask — MUST reflect reality
  "subjects": ["team:atlas"],
  "categories": ["process"],
  "justification": "Stated by @dana as a standing rule; likely relevant to future deploy questions",
  "source_episode_ids": ["…"],               // episodes already registered for this session
  "valid_at": null }                          // when known
```

Response is the **policy verdict**, not a bare ack:

```jsonc
{ "decision": "stage",            // allow | stage | ask | deny
  "memory_id": "…",               // present for allow/stage
  "status": "staged",
  "ask_prompt": null,             // for ask: the exact question to relay to the user
  "reason": "agent_observed writes stage by default in this scope" }
```

`ask` is a two-step: the tool returns the confirmation question, the agent relays it in-surface ("Want me to remember that team Atlas only deploys on Tuesdays?"), and the confirmation comes back via `memory_confirm(pending_id, approved)`. `deny` returns the rule id and a human-readable reason — the agent is expected to *not* silently retry.

### `memory_reinforce`

```jsonc
{ "memory_id": "…", "signal": "useful", "note": "used to answer deploy-window question" }
```

Explicit usefulness feedback (the agent used the memory and it was right / the user reacted well). Complements the implicit signals ([doc 03 §4](03-lifecycle.md)). A `signal: "wrong"` variant *lowers* confidence and queues contradiction review without the agent needing forget powers.

### `memory_forget`

```jsonc
{ "memory_id": "…", "reason": "user asked to forget this", "mode": "archive" }  // archive | tombstone
```

Policy-gated: by default agents may only forget memories in their own `agent:*` scope or ones they authored that are still `staged`; anything else routes to `ask`. Users forget via the product surfaces (review UI, or in-chat "forget that", which the agent fulfills with this tool + `on_behalf_of`).

### `memory_promote`

```jsonc
{ "memory_id": "…", "target_scope": "workspace/T024B",
  "justification": "dana asked to share the deploy rule with all teams" }
```

Scope promotion. Almost always returns `ask` ([doc 05 §3](05-policy.md)). The one tool where the agent *does* name a scope — the service validates the target is on the agent's writable set.

### `memory_status`

```jsonc
{ "memory_id": "…" }   // or { "subject": "user:dana" } / { "scope": "channel/C0DEP" }
```

Read-only introspection: lifecycle state, provenance summary, event history digest. Powers in-chat transparency ("what do you know about me?" must be answerable *in the surface*, not only in an admin UI).

### `memory_observe`

```jsonc
{ "content": "payments/ stores money as integer cents — never floats",
  "paths": ["payments/ledger.py", "payments/models.py"],   // files the observation is about
  "ref": null,                                             // optional deep-link back to the session
  "occurred_at": null }                                    // when observed; defaults to now
```

Dev-time episode registration ([ADR-0012](adr/0012-dev-time-agent-surface.md)): the personal `surface:ide` client has no platform-side subscriber, so it registers its own episodes rather than relying on ingestion (§5's `POST /v1/episodes` is platform-only). The episode is `source_kind=dev_observation`, author = the on-behalf-of developer, at a **lower trust base** than platform-verified sources — usable only by agents enrolled as writers on the flow's `devsession/<id>` container. It still traverses the full write pipeline (PII, policy, staged-by-default), and the touched `paths` drive module-scope routing and chain inclusion ([ADR-0010](adr/0010-module-boundary-detection.md), [ADR-0013](adr/0013-dev-time-routing-defaults.md)).

## 2. Read paths

### 2.1 Ambient recall (context block)

Platform code (not the agent) calls `POST /v1/context-block` at session start / periodically per turn-batch:

```jsonc
{ "principal": {"agent": "agent:sage", "on_behalf_of": "user:dana"},
  "flow": {"surface": "slack", "container": "channel/C0DEP", "participants": ["user:dana","user:li"]},
  "focus": "last 6 messages of the thread…",     // optional retrieval focus
  "token_budget": 1200 }
```

The service resolves the scope chain, runs retrieval (§3), and assembles a bounded block, Zep-style:

```
<memoramum scope="#deploys" generated="2026-05-14T09:12Z">
INVARIANTS
- Never trigger production deploys from chat commands. [org rule]
FACTS (current)
- Team Atlas deploys to production only on Tuesdays. [workspace-shared; from #deploys, Mar 2026]
- dana prefers deploy summaries as threads, not channel messages. [about dana]
STAGED (unconfirmed — verify before relying on these)
- Atlas may be adopting feature flags for deploys. [staged; single observation, May 2026]
</memoramum>
```

Rules: invariants first, then active facts (scored order), then a clearly-fenced staged section; every line carries its scope/provenance hint; validity dates shown when a fact has a window. One `READ` event per block, listing delivered memory ids.

### 2.2 Deliberate recall

`memory_recall` mid-task, for anything the ambient block didn't anticipate. The prompt contract (§4) tells agents when to reach for it: before answering questions about people, teams, or process; before repeating expensive discovery; when the user references something "we discussed."

The two paths are complementary, per the LangMem hot-path/background framing: ambient recall gives floor-level continuity with zero agent effort; deliberate recall gives depth on demand.

## 3. Retrieval scoring

Generative-Agents-shaped composite over hybrid candidates:

```
candidates = top-K by hybrid relevance
             (vector cosine over content_embedding  ⊕  BM25/tsvector lexical, RRF-fused)
             within permitted scope chain, status ∈ {active, invariant, staged*}

score(m) = w_rel · relevance(m)          # normalized hybrid score
         × R(m)                          # retention exp(-t/S) — recency/decay (doc 03 §5)
         × trust_weight(m)               # trust_score, floored by read-context policy
         × status_weight(m)              # invariant 1.2 · active 1.0 · staged 0.6
         × scope_proximity(m)            # narrower scope in chain ranks above broader (channel beats org)
```

- Lexical search is a first-class leg, not an afterthought — exact tokens (team names, service names, MR numbers) are where pure-vector recall fails.
- `scope_proximity` encodes "the channel's own memory beats the org-wide default" — specific context wins over general.
- All weights are policy-tunable per agent; defaults above.
- Retrieval never *mutates* trust/confidence; it updates `last_accessed_at`/`access_count` and emits `READ`.

Access control is applied **before** scoring (scope-chain intersection + attribute rules, [doc 05 §4](05-policy.md)) — never post-filtering an over-fetched candidate set, which both starves top-k and leaks via timing.

## 4. The prompt contract

Shipped with the MCP server as a system-prompt snippet; per-agent policy can extend it. Abridged normative content:

**Remember when:**
- the user explicitly asks ("remember / note / don't forget") → `origin_kind: explicit_user_ask`, verbatim-faithful content;
- the user corrects you or states a durable preference/constraint;
- you learn a stable fact about a team, process, or system that future sessions will need;
- you complete something the hard way and the lesson generalizes → `episodic`, and say so in `justification`.

**Do not remember:**
- secrets, credentials, tokens (these are policy-denied anyway — do not attempt);
- transient state (build currently red, someone on vacation) unless with explicit `valid_at`/short TTL intent;
- third-party personal information beyond what the flow requires — especially health, beliefs, private life (`ask` at best);
- speculation phrased as fact — mark uncertainty in content ("dana *may* prefer…") or don't write.

**Recall when:**
- before answering anything about a person, team, process, or past decision — check first, don't guess;
- when the user references shared history ("like last time", "the usual");
- before redoing discovery you may have done before.

**Conduct:**
- honor the verdicts: `deny` is final for this write — do not rephrase to evade; relay `ask` questions verbatim;
- never claim to have remembered or forgotten something unless the tool confirmed it;
- prefer citing provenance for memory-derived claims ("per @dana in #deploys in March");
- treat `[staged]` items as hypotheses: verify before acting on them in consequential ways;
- if a tool returns `unavailable`, say "I can't check my memory right now" — don't guess, and don't claim memory you couldn't reach ([doc 07 §5](07-operations.md)).

The contract is persuasive; the *enforcement* is server-side policy ([doc 05](05-policy.md)) — the design assumes agents will sometimes ignore instructions, and nothing in the security model depends on them not doing so.

## 5. REST surface (platform/admin)

REST callers authenticate with **bearer tokens bound to principals** ([ADR-0014](adr/0014-bearer-token-rest-auth.md)): the deployment issues each platform component, admin, or reviewer a token, and the token — not a caller-asserted header — names the `actor` of every call (`401` without a valid token; `403` when a request asserts a different actor than its token is bound to). `on_behalf_of` stays caller-asserted, the same trust extended to the surface integration that launches the MCP server with the principal pair in its environment ([ADR-0005](adr/0005-standalone-service-mcp.md)). `/healthz` is open; with no tokens configured the facade runs an unauthenticated dev-mode shim that refuses to bind beyond loopback.

| Endpoint | Purpose |
|---|---|
| `POST /v1/context-block` | ambient recall (§2.1) |
| `POST /v1/episodes` | register source episodes (platform ingestion, not agents) |
| `POST /v1/membership-sync` | per-surface sync heartbeat from platform ingestion; feeds the retrieval staleness bound ([doc 07 §5](07-operations.md)) |
| `GET /v1/memories/{id}` / `GET /v1/memories/{id}/history` | record + full event history (Mem0-style changelog) |
| `GET /v1/subjects/{principal}/memories` | "everything about X" (review UI, DSAR) |
| `GET /v1/scopes/{id}/memories` | scope inventory (channel admin view) |
| `GET /v1/review/staged` / `POST /v1/memories/{id}/review` | staged-triage queue; confirm/reject by a scope member ([doc 03 §4](03-lifecycle.md)) |
| `GET /v1/review/contradictions` / `POST /v1/review/contradictions/{id}` | held-contradiction queue; resolve supersede / keep-both / reject ([doc 03 §3](03-lifecycle.md)) |
| `GET /v1/review/pending` / `POST /v1/review/pending/{id}` | open `ask` confirmations; approve/decline by the confirmer ([doc 05 §3](05-policy.md)) |
| `GET /v1/policies` / `POST /v1/policies` | versioned policy documents, YAML in / canonical JSON out ([doc 05 §5](05-policy.md)) |
| `POST /v1/policies/simulate` | simulation mode: evaluate a proposed policy against recent decisions ([doc 05 §5](05-policy.md)) |
| `POST /v1/module-paths` | admin: set the path-glob → module-scope mapping ([ADR-0010](adr/0010-module-boundary-detection.md)) |
| `POST /v1/memories/{id}/forget` | user/review-UI forget ([doc 03 §6](03-lifecycle.md); agents use the `memory_forget` tool) |
| `POST /v1/erasure-requests` / `GET /v1/erasure-requests/{id}` | GDPR pipeline; completed requests carry the signed attestation ([doc 06 §2](06-audit-privacy-security.md)) |
| `POST /v1/quarantine` | provenance-based bulk revoke ([doc 06 §3](06-audit-privacy-security.md)) |
| `GET /v1/quarantine` / `POST /v1/quarantine/{id}` | quarantined-lineage review: restore or tombstone ([doc 06 §3](06-audit-privacy-security.md)) |
| `POST /v1/agents/{agent}/freeze` | break-glass write-freeze ([doc 05 §5](05-policy.md)) |
| `GET /v1/audit/events` | filtered event-log queries (admin) |
| `GET /v1/metrics` | the [doc 07 §4](07-operations.md) metrics, one store-derived snapshot (admin/auditor; [ADR-0008](adr/0008-metrics-from-the-store.md)) |

Continue with [doc 05 — Policy](05-policy.md): the layer that decides every verdict this interface returns.
