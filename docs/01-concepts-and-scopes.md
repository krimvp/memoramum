# 01 — Concepts & scopes

This document defines the core nouns of the system and the scope model — the part of the design that answers *"in the context of some flow or context"*: which memories exist where, and who can see them.

See the [glossary](../README.md#glossary) for one-line definitions; this document is the normative description.

---

## 1. Memory

A **memory** is the atomic unit of the system: a piece of knowledge an agent can recall later. Design commitments:

- **Natural-language-first.** The canonical content of a memory is a short, self-contained natural-language statement ("Team Atlas deploys to production only on Tuesdays"). Structured attributes (entities, tags, embeddings) are *derived* and can be regenerated; the text is the source of truth. This follows the lesson from Mem0/Zep/LangMem: LLMs both produce and consume memory best as text, and schemas ossify faster than facts do.
- **Atomic.** One claim per memory. Compound observations are split at extraction time. Atomicity is what makes supersession, contradiction detection, deduplication, and per-fact access control tractable.
- **Immutable content, mutable state.** Once written, a memory's content never changes. Corrections and updates create a *successor* memory and close the predecessor's validity window ([doc 03](03-lifecycle.md)). Lifecycle status, decay strength, and trust do change in place — they are bookkeeping, not knowledge.
- **Exactly one scope** (§3) and **exactly one kind** (§2).

What a memory is *not*: it is not a document store, not a RAG index over files, and not conversation history. Raw conversation is kept as **episodes** (provenance atoms, [doc 02](02-data-model.md#3-episodes)); documents belong in whatever document/RAG system the platform already has. Memoramum stores *distilled knowledge with provenance*.

## 2. Kinds of memory

Four kinds, adapted from the cognitive taxonomy used by LangMem and the CoALA framework. The kind determines the default lifecycle, retrieval behavior, and consolidation strategy — it is not just a label.

| Kind | What it holds | Example | Update model | Typical origin |
|---|---|---|---|---|
| `semantic` | Facts, preferences, constraints about the world | "Team Atlas deploys only on Tuesdays" | supersede-on-contradiction | any |
| `episodic` | What happened; successful/failed interactions kept as exemplars | "On 2026-03-12, rolling back migration 0042 required manually clearing the cache" | append; summarize in consolidation | `agent_observed` |
| `procedural` | How the agent should behave; distilled operating instructions | "When reviewing MRs that touch `deploy/`, always check the deploy calendar" | versioned replacement | `consolidated`, `explicit_user_ask` |
| `profile` | Bounded, schema'd document about one subject | dana's profile: timezone, notification preferences, role | update-in-place (single doc per subject per scope) | any |

Notes:

- `profile` is deliberately the one exception to "immutable content": it is a *current-state* document (LangMem's profile pattern) whose edits are fully recorded in the event log. Use it for bounded state where you always want exactly the latest value and history belongs in the audit trail, not in retrieval.
- `procedural` memories are what close the loop from *experience* to *behavior*: consolidation can distill repeated episodic lessons into a procedural rule ([doc 07 §2](07-operations.md)). They are the highest-risk kind (they steer the agent), so policy typically routes them through `ask` mode ([doc 05](05-policy.md)).
- `episodic` memories are the raw material for reflection. They decay fastest by default.

## 3. Scopes

A **scope** is the visibility container a memory lives in. The scope model is the answer to the hard requirement: *channel-specific memories, workspace-shared memories, and memories that cross surfaces* — without leaking anything by accident.

### 3.1 The scope tree

Scopes form a forest of four families under a single org root:

```
org:acme
├── surface:slack
│   └── container:workspace/T024B          (Slack workspace)
│       └── container:channel/C0DEP        (#deploys)
│           └── container:thread/1709481600.123
├── surface:gitlab
│   └── container:project/platform-api     (repo)
│       └── container:mr/482               (one MR)
├── surface:ide                            (personal dev-time agents — ADR-0012)
│   └── container:devsession/9f3a          (one local session — private)
├── module:platform-api/payments          (module conventions — parallel family, ADR-0009)
├── subject:user/dana                      (about dana — cross-surface)
├── subject:team/atlas                     (about a team — cross-surface)
└── agent:sage                             (Sage's private working knowledge)
```

- **Container scopes** mirror the natural containers of each surface. They nest: a memory in `channel/C0DEP` is *narrower* than one in `workspace/T024B`. The tree is extensible — a new surface adds a subtree, nothing else changes.
- **Subject scopes** hold memories *about* a person or team, independent of where they were learned. They are what lets Sage and Marge share "dana prefers small MRs" without either surface owning that fact. Subject scopes are the GDPR-sensitive family: everything in `subject:user/dana` is enumerable for a data-subject request ([doc 06](06-audit-privacy-security.md)).
- **Agent scopes** are an agent's private notebook: task tactics, self-observations, working state. Never readable by other agents by default.
- **Module scopes** are a parallel family for monorepo-module conventions: `module:<project>/<path>` (e.g. `module:platform-api/payments`), with `parent_scope_id` the project container, so project enrollment and membership govern them by the same subtree walk. They hold conventions about one module independent of any single MR, and — like subject scopes — are pulled into a read's scope chain *contextually*: by the paths a flow touches (§3.3), not by where it runs. See [ADR-0009](adr/0009-module-scope-family.md).
- **`surface:ide`** is the personal dev-time surface (IDE- or CLI-hosted agents), with per-session `container:devsession/<id>` scopes at `trust_class=private` — a developer's local session is theirs. Unlike the platform surfaces, its episodes are client-registered rather than platform-ingested ([doc 04 §1](04-agent-interface.md), [ADR-0012](adr/0012-dev-time-agent-surface.md)).

Scope IDs are opaque strings with the `family:qualifier` shape shown above; the hierarchy lives in a `parent_scope_id` relation, not in string parsing ([doc 02 §1](02-data-model.md)).

### 3.2 Rules

1. **One memory, one scope.** A memory lives in exactly one scope. "Visible in several places" is achieved by *placement in a broader scope*, never by multi-tagging. (Rationale: multi-scope tagging makes access review — "what can leak into this channel?" — a join over everything; single placement makes it a subtree walk. See [ADR-0002](adr/0002-single-scope-per-memory.md).)
2. **Default to the narrowest scope.** A memory is written to the narrowest scope that contains the information's source: learned in a channel → channel scope; learned about a person → that person's subject scope only if policy explicitly routes it there.
3. **Broadening is promotion.** Moving a memory up the tree (channel → workspace) or across families (channel → subject) is a **promotion**: an explicit operation, checked against policy, possibly requiring user confirmation, always audited ([doc 03 §4](03-lifecycle.md), [doc 05 §3](05-policy.md)). Narrowing (splitting a memory down into a narrower scope) is allowed without ceremony.
4. **The source-visibility invariant.** A memory must never surface to a principal who could not access its *source episodes* at the time of the request. Concretely: a memory derived from a private-channel message is only readable by current members of that channel, *checked at retrieval time* — someone who leaves the channel loses access to memories derived from it, immediately. This is Slack AI's core security contract, adopted wholesale. Promotion is the *only* mechanism that relaxes it, which is exactly why promotion is gated and audited.
5. **Isolation ordering.** For default policy purposes scopes have a trust ordering: `DM / private channel ≫ public channel ≫ workspace ≫ org`, and *externally shared containers* (Slack Connect channels, public repos) rank **below** internal public ones. Policy defaults derive from this ordering (e.g. nothing auto-promotes out of a private channel).

### 3.3 Scope chains — how reads see the tree

A read never queries one scope; it resolves a **scope chain**: the ordered list of scopes relevant to the current flow, filtered by what the requesting principal may read.

Example — Sage answering in `#deploys`:

```
chain = [ thread/1709…,            # current thread
          channel/C0DEP,           # the channel
          workspace/T024B,         # workspace-shared
          org:acme,                # org-wide
          subject:user/dana,       # participants' subject scopes (policy-gated)
          agent:sage ]             # own private scope
```

Example — Marge reviewing MR !482 in `platform-api`:

```
chain = [ mr/482, module:platform-api/payments, project/platform-api, org:acme,
          subject:user/<author>, agent:marge ]
```

Two selectors pull parallel-family scopes into a chain contextually: **participants** add their `subject:*` scopes (policy-gated), and the **paths a flow touches** add the `module:*` scopes those paths map to — resolved from the MR diff (or a dev-session's touched files) through the `module_paths` glob mapping ([ADR-0010](adr/0010-module-boundary-detection.md)), inserted narrower-than-project so a module's own convention outranks the repo-wide default. Paths that match no glob map to no module and fall back to the project scope already in the chain. One mechanism, two selectors — no new chain machinery.

Marge's chain contains **no Slack scopes**. The only way the Tuesday-deploys memory reaches Marge is that it was *promoted* to a scope both surfaces share (`org:acme`, or a shared team scope) — which is exactly what step 3 of the [worked scenario](../README.md#the-worked-scenario) does. Cross-surface sharing is therefore not a special mechanism; it falls out of scope placement.

Chain resolution is service-side ([doc 04 §2](04-agent-interface.md)): the agent presents its context (surface, container, participants), the service computes the chain and intersects it with the principal's read permissions ([doc 05 §4](05-policy.md)). Agents cannot request arbitrary scopes.

## 4. Principals

Three kinds of principal act on the system; every event records which one (and the full pair when acting on behalf of someone):

| Principal | Example | Notes |
|---|---|---|
| `user` | `user:dana` | Humans. The only principals who can confirm `ask`-mode decisions. |
| `agent` | `agent:sage`, acting `on_behalf_of user:dana` | Agents always act *as themselves*; when a live user is present, the on-behalf-of identity is recorded and used for access checks (the *intersection* of agent and user permissions applies — an agent enrolled in a scope still can't read it for a user who can't). |
| `system` | `system:consolidator` | Background jobs. Fully audited like any other actor; their "justification" is the rule that fired. |

Agents must be **enrolled** in a scope to read or write it (a relation tuple, [doc 05 §4](05-policy.md)). Enrollment is how "some systems might not have access to certain memories, others do" is configured: Marge is enrolled as a reader of `org:acme` shared scopes but not of any Slack container scope.

## 5. What the scenario looks like in these terms

| Scenario step | Concepts involved |
|---|---|
| Sage observes dana's message | episode created in `channel/C0DEP`; extraction proposes a `semantic` memory, `origin=agent_observed`, scope `channel/C0DEP`, status `staged` |
| Reinforcement | consolidator records re-observation; status → `active` |
| "Tell the other teams" | promotion `channel/C0DEP → workspace/T024B`; crossing into a shared scope → `ask` → dana confirms |
| Marge's warning | Marge's scope chain includes the shared scope; retrieval-time check passes (Marge enrolled, MR author may read); read event logged |
| "We deploy daily now" | contradiction: old memory `invalid_at` set, status → `deprecated`, successor created with `derived_from` link |
| Admin audit | event-log traversal: read event → memory → promotion event → reinforcements → episode → dana's message |

Continue with [doc 02 — Data model](02-data-model.md), which gives these concepts their concrete shape.
