# 05 — Policy

The policy layer is a **separate subsystem** that the memory service depends on — the requirements ("some systems might not have access to certain memories", "configurable", staged/ask flows) are unimplementable without it. It is designed here because Memoramum is its first consumer, but nothing in it is memory-specific: it is an agent-governance layer, and other agent capabilities (tool access, escalation rules) can grow into it.

Two halves, enforced at different points:

- **Learning policy** — governs *writes*: what each agent may learn, where it routes, what requires a human.
- **Access policy** — governs *reads* (and enrollment): which principals may see which scopes, under which attribute conditions.

Guiding principles (synthesized from the prior art — Bedrock AgentCore's declarative strategies, Claude Code's layered allow/ask/deny, Slack's retrieval-time checks):

1. **Default deny.** An agent with no learning policy attached learns nothing; an agent not enrolled in a scope reads nothing from it.
2. **Three-plus-one verdicts.** Every write resolves to `allow | stage | ask | deny`; every read check to `allow | deny`. `ask` is first-class, not an error path.
3. **Layered, with a floor.** Org policy > surface policy > agent policy > user preference. Lower layers can only *tighten*, never loosen, and the org layer is un-overridable.
4. **Decisions are data.** Every evaluation emits a `POLICY_DECISION` event with the winning rule id ([doc 02 §5](02-data-model.md)). Policy changes are themselves evented (`POLICY_CHANGE`).

---

## 1. Policy objects

```yaml
# One learning policy, attached to an agent (or surface, or org layer).
policy: sage-slack-default
layer: agent                     # org | surface | agent
applies_to: agent:sage

learning:
  default: deny                  # anything not matched below is denied
  strategies:
    - name: team-process-facts
      kinds: [semantic]
      categories: [process, tooling, schedule]
      origin_kinds: [agent_observed, llm_inferred]
      route:
        scope: source            # write to the scope of the source episode (the default)
      decision: stage            # -> staged tier
      ttl: null
    - name: explicit-asks
      kinds: [semantic, procedural, profile]
      origin_kinds: [explicit_user_ask]
      decision: allow            # skips staging (doc 03 §2)
    - name: personal-preferences
      kinds: [profile, semantic]
      categories: [preference]
      subjects: participants     # only about people present in the flow
      route: { scope: subject }  # routes to subject:user/<id>
      decision: stage
      ttl: 365d
    - name: codebase-conventions # dev-time observations about a module (ADR-0013)
      kinds: [semantic, procedural]
      categories: [codebase_convention]
      origin_kinds: [agent_observed, llm_inferred]
      route: { scope: module }   # touched module scope; fallback project (ADR-0009/0010)
      decision: stage
    - name: task-tactics         # an agent's own working tricks stay private (ADR-0013)
      kinds: [semantic, procedural]
      categories: [task_tactic]
      route: { scope: agent }    # the writing agent's own agent:* notebook
      decision: stage
    - name: sensitive-personal
      categories: [health, beliefs, relationships, location_history]
      decision: ask              # never silent, regardless of origin
      sensitivity_floor: confidential
    - name: secrets
      categories: [credentials, secrets]
      decision: deny             # hard floor; also enforced by the PII pipeline (doc 06 §4)

  procedural_writes: ask         # kind=procedural always asks (it steers behavior)

promotion:                       # scope-promotion rules (doc 03 §4)
  to_shared_scope: ask           # channel -> workspace/org, or -> subject:*
  from_private_scope: deny       # nothing leaves a DM/private channel automatically
  to_module_scope: ask           # mr/dev-session -> module (confirmer: module_owner; ADR-0011)
  to_project_scope: ask          # module -> project, stricter (confirmer: maintainer; ADR-0011)
  confirmers: [scope_member]     # who may confirm: any member of the source scope

retention:
  overrides:                     # per-category TTL / deletion mode
    - { categories: [health], ttl: 90d, expiry_mode: tombstone }

read:                            # gates (tighten downward) + weights (narrowest setter wins)
  include_staged: true
  trust_floor: 0.3               # minimum trust_score retrievable in this agent's contexts
  sensitivity_ceiling: internal  # this agent's surfaces never see confidential+ memories
  weights:                       # exponents on the doc 04 §3 factors; 1.0 = default (ADR-0018)
    status: 2.0                  # bury staged results without excluding them
    scope_proximity: 1.0
```

Semantics:

- **Strategies are the allowlist.** A candidate write is matched against strategies top-down (within a layer); first match wins *within the layer*, then layers combine by strictness (§2). No match anywhere → `deny` (principle 1). This is the Bedrock insight: *what gets learned is declarative configuration, not emergent behavior.*
- **`route`** decouples "what the agent asked" from "where it lands": policy, not the agent, decides that preference-facts go to subject scopes. Route targets are `source` (the source episode's scope, the default), `subject`, `module` (the touched module scope, fallback project — [ADR-0009](adr/0009-module-scope-family.md)/[ADR-0013](adr/0013-dev-time-routing-defaults.md)), and `agent` (the writing agent's private scope). The agent's requested scope can only be narrowed by routing, never broadened — with one named exception: dev-time `codebase_convention` observations route *outward* from the private `devsession` container to the touched `module` scope, the deliberate sharing carve-out of [ADR-0013](adr/0013-dev-time-routing-defaults.md).
- **Categories** are the vocabulary shared with `memories.categories` — assigned at extraction by the classifier ([doc 06 §4](06-audit-privacy-security.md)), matched by policy. The org layer owns the category taxonomy.

## 2. Evaluation

Write pipeline (every `memory_remember`, background-extraction batch, and scope promotion):

```
candidate ──▶ classify (categories, sensitivity, subjects, PII scan)
          ──▶ evaluate layers: org → surface → agent → user-pref
                 each layer yields a verdict; combine = strictest wins
                 (deny > ask > stage > allow)
          ──▶ emit POLICY_DECISION event (verdict, rule ids per layer)
          ──▶ enact: write / stage / return ask-prompt / return denial
```

- **Strictest-wins composition** is what makes the org floor real: an agent-layer `allow` cannot beat an org-layer `ask`. A *user preference* ("don't remember things about me from Slack") is the fourth layer — users can always tighten what is learned *about them* (`subjects` containing their id), never loosen org policy.
- The evaluation engine is deliberately boring: strategy matching is structural (kind/category/origin/scope-class set membership), implementable as SQL/table lookups. An OPA/Rego escape hatch exists for genuinely attribute-heavy conditions (time-bounded rules, request-context conditions), but the design goal is that 95% of policy fits the declarative YAML shape above — reviewable by a human in a diff.

## 3. Promotion policy

Scope promotion ([doc 03 §4](03-lifecycle.md)) is evaluated as a *write into the destination scope*, plus promotion-specific gates:

| Crossing | Default verdict |
|---|---|
| narrower → broader within a surface (channel → workspace) | `ask` (confirmer: member of source scope) |
| container → subject scope (about-a-person) | `ask` (confirmer: **the subject** — dana confirms what is recorded about dana in shared view) |
| `mr → module` (or dev-session → module) | `ask` (confirmer: **`module_owner`** — the module-owner relation tuple on the destination module scope) |
| `module → project` | `ask` (confirmer: **`maintainer`** on the project scope — stricter, a repo-wide assertion needs a repo-wide steward) |
| anything out of `trust_class=private` | `deny` (org floor; requires org-admin override) |
| anything out of `trust_class=shared_external` | `deny` by default |
| broader → narrower | `allow` |

The two monorepo crossings and their tuple-based confirmers are recorded in [ADR-0011](adr/0011-module-promotion-crossings.md); policy documents may tighten (never loosen) them via the `to_module_scope` / `to_project_scope` keys (§1). The confirmation itself is an event (`CONFIRM`, actor = the human), and the promoted memory's provenance `activity` records the `policy_decision_id` — the full chain "who allowed this to be shared and under which rule" is reconstructible.

## 4. Access policy (reads & enrollment)

Two layers, evaluated in order, both *before* retrieval scoring ([doc 04 §3](04-agent-interface.md)):

### 4.1 Scope membership — ReBAC-shaped

Relation tuples, Zanzibar-style but stored as plain Postgres tables for the single-org case ([ADR-0004](adr/0004-postgres-reference-stack.md)); the schema is deliberately isomorphic to SpiceDB/OpenFGA models so migration is a data export, not a redesign:

```
scope_relations(scope_id, relation, principal)
  ('channel/C0DEP',   'member',        'user:dana')      -- synced from Slack, or resolved live
  ('workspace/T024B', 'member',        'user:dana')
  ('org:acme',        'reader_agent',  'agent:marge')    -- agent enrollment
  ('channel/C0DEP',   'reader_agent',  'agent:sage')
  ('channel/C0DEP',   'writer_agent',  'agent:sage')
  ('subject:user/dana','owner',        'user:dana')
  ('module:platform-api/payments','module_owner','user:li')   -- confirms mr -> module promotions (ADR-0011)
  ('project/platform-api','maintainer','user:dana')           -- confirms module -> project (repo-wide)
```

Read permission for a `(agent, on_behalf_of user)` pair on scope S:

```
readable(S) =  enrolled(agent, S)                       -- reader_agent on S or an ancestor
            ∧  ( user is NULL  ∨  member(user, S) )      -- on-behalf-of user could see the source
```

The **intersection rule** implements the source-visibility invariant ([doc 01 §3.2](01-concepts-and-scopes.md)): an agent enrolled org-wide still cannot recall channel-scoped memories for a user who isn't in that channel. Membership for surface containers is checked at retrieval time — against a near-real-time sync of surface membership (with the surface's own API as the authority for cache-miss/verification). Membership *changes* invalidate nothing at rest; they simply change what the next read returns. Syncing surfaces heartbeat a per-surface watermark after each cycle; retrieval enforces the [doc 07 §5](07-operations.md) staleness bound against it (membership authored directly in the service has no sync to go stale).

Subject scopes: `owner` (the subject) always reads; agents need `reader_agent` enrollment *and* the subject's user-preference layer not opting out; other humans read only via audit/DSAR roles.

### 4.2 Attribute rules — the read-side conditions

Applied to the candidate scope set and to individual memories:

- `sensitivity_ceiling` per agent/surface (a memory tagged `confidential` never enters a `shared_external` Slack Connect channel's context block, regardless of scope math);
- `trust_floor` per read context — high-stakes flows (an agent about to take an action) can require `trust_score ≥ 0.7` and `status ≠ staged`;
- category deny-lists per surface (e.g. nothing categorized `hr_confidential` is ever retrievable by non-HR agents);
- `as_of`/history queries require an elevated audit role — time travel is an audit feature, not an agent feature.

Those four are **gates**: they decide what may be retrieved at all, and they compose strictest-wins like every other axis here. The `read.weights` block is the other half of §1's `read:` section and is **not** a gate — the [doc 04 §3](04-agent-interface.md) exponents only reorder what the gates already permitted, so "strictest" has no meaning for them and they compose differently: the org layer's weights are final, and below org the narrowest layer that sets a weight wins ([ADR-0018](adr/0018-retrieval-weights-are-policy.md)). Weights can never widen a read; a policy that wants staged memories out of a high-stakes context sets `include_staged: false`, not `weights.status: 0`.

## 5. Administration

- Policies are **versioned documents** (stored in the service, YAML in, canonical JSON out); every change emits `POLICY_CHANGE` with a diff, and evaluations record the policy *version* they ran against — "what would this decision have been last month" is answerable.
- **Simulation mode**: a proposed policy can be evaluated against the last N days of `POLICY_DECISION` events to show what would have changed (writes newly denied, reads newly blocked) before activation.
- **Break-glass**: org admins can quarantine an agent entirely (all writes `deny`, reads unaffected or fully off) — one flag, evented, for incident response ([doc 06 §3](06-audit-privacy-security.md)).

## 6. The worked scenario against this policy

| Step | Evaluation |
|---|---|
| Sage's extraction proposes the Tuesday rule | matches `team-process-facts` (semantic/process/agent_observed) → `stage`; routed to source scope `channel/C0DEP` |
| dana: "remember I prefer thread summaries" | matches `explicit-asks` → `allow`, but routing: category `preference` + subject dana → `subject:user/dana`; subject-scope write about the asker herself → no `ask` needed |
| Sage tries to note a colleague's medical leave | category `health` → `sensitive-personal` → `ask`; user declines → `REJECT` event, nothing stored |
| Promotion of the Tuesday rule to workspace | `promotion.to_shared_scope: ask` → dana (scope member) confirms |
| Marge recalls in MR !482 | Marge `reader_agent` on `org:acme` ✓; MR author member of org ✓; memory sensitivity `internal` ≤ ceiling ✓; trust 0.8 ≥ floor ✓ → delivered, `READ` event |

Continue with [doc 06 — Audit, privacy & security](06-audit-privacy-security.md).
