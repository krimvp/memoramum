# ADR-0013 — Dev-time codebase-convention writes route to shared scopes

**Status:** accepted · **Context docs:** [01 §3](../01-concepts-and-scopes.md), [05 §1](../05-policy.md), [03 §4](../03-lifecycle.md)

## Decision

A **named exception** to "agent scopes are private by default" ([doc 01 §3.1](../01-concepts-and-scopes.md)): a dev-time observation ([ADR-0012](0012-dev-time-agent-surface.md)) classified with category `codebase_convention` routes to the **touched module scope** ([ADR-0009](0009-module-scope-family.md)) — fallback to the project scope when no module matches — with decision `stage`. Personal task tactics (category `task_tactic`) stay in the agent's own `agent:*` scope. The mechanism is [doc 05 §1](../05-policy.md)'s `route` — policy, not the agent, decides where a write lands; this ADR fixes the default *strategy shape*. Two route targets join the policy vocabulary: **`module`** (the touched module scope, fallback project) and **`agent`** (the writing agent's private scope), beside the existing `source` and `subject`. A worked policy YAML example — a `codebase-conventions` strategy analogous to `personal-preferences` — is added to [doc 05 §1](../05-policy.md).

## Alternatives considered

- **Keep conventions in `agent:*` and rely on promotion.** A convention nobody else can see is a convention nobody can reinforce or promote — the surface would be useless to the team, since the whole value is a shared, reusable codebase norm.
- **Route straight to the project scope.** Over-broad: a convention about `payments/` is asserted repo-wide from a single local observation. The module scope is the narrowest scope that contains the information's source ([doc 01 §3.2](../01-concepts-and-scopes.md) rule 2) — the design's default.

## Why module-routing wins here

1. **Narrowest-correct placement.** Rule 2 says write to the narrowest scope that contains the source; for a path-scoped convention that is the module, not the agent's notebook and not the whole project.
2. **Staging preserves quarantine.** Routing to a shared scope does not skip the safety layers — the write still enters `staged` ([ADR-0003](0003-staged-tier-default.md)) at a lower `dev_observation` trust base, so a team-visible convention still has to earn `active` through reinforcement.
3. **Personal stays personal.** `task_tactic` is the counter-example that keeps the exception narrow: an agent's own tactics route to `agent:*` exactly as before.

## Costs accepted

This is a **carve-out of the agent-scope privacy default, and it is stated plainly**: a developer's local observation becomes visible — `staged`, fenced, low-trust, but visible — to the module's team without a separate promotion step. The routing decision is therefore itself the sharing boundary, so it must be reviewable: it emits a `POLICY_DECISION` event and surfaces in staged-triage, and the exception is deliberately narrow (only `codebase_convention`; `task_tactic` and everything else stay agent-private). A mis-classification that tags a personal note `codebase_convention` over-shares it into the module scope — bounded by staging and reversible by forgetting, but a real edge the classifier's precision must earn against.
