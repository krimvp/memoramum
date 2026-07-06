# ADR-0011 — Distinct `mr→module` and `module→project` promotion crossings

**Status:** accepted · **Context docs:** [03 §4](../03-lifecycle.md), [05 §3](../05-policy.md), [01 §3](../01-concepts-and-scopes.md)

## Decision

Two **distinct** crossing types join the promotion table ([doc 05 §3](../05-policy.md)), each with its own confirmer:

- **`mr → module`** (equivalently dev-session → module): default `ask`; **confirmer = `module_owner`** — a new explicit relation tuple `(module scope, 'module_owner', principal)` in `scope_relations`. CODEOWNERS can seed the tuples where present; the tuple is authoritative, consistent with how membership tuples are synced today ([doc 05 §4](../05-policy.md)).
- **`module → project`**: a distinct, **stricter** crossing — default `ask`, **confirmer = `maintainer`** relation on the *project* scope. A repo-wide assertion needs a repo-wide steward, not one module's owner.

Promotion still re-runs the full write pipeline against the destination scope ([doc 03 §4](../03-lifecycle.md)); it emits `POLICY_DECISION` / `CONFIRM` events exactly as every other promotion. Policy documents may **tighten** (never loosen) both crossings via `promotion:` keys `to_module_scope` / `to_project_scope`, valued `ask` or `deny`.

## Alternative confirmer candidates considered

- **CODEOWNERS lookup at confirm time**: resolve the confirmer by parsing CODEOWNERS in the decision path. Rejected — an external dependency in the hot decision path. Tuples keep confirmer resolution a plain table lookup, like every other ReBAC question ([ADR-0004](0004-postgres-reference-stack.md)); CODEOWNERS seeds the tuples out of band.
- **"Any past contributor" of the module/project**: too broad to mean consent — having once edited a file is not authority to publish a convention over it.

## Why tuple-based, two-tier confirmers win here

1. **Blast radius picks the steward.** A module convention crossing into `module:*` affects one module's team; a convention crossing into `project/*` affects everyone in the repo. Two crossings with two confirmer relations make that asymmetry explicit instead of overloading one role.
2. **Confirmer resolution stays a table lookup**, the same shape as `scope_member` / `owner` confirmers already in [doc 05 §3](../05-policy.md) — no new evaluation model, no external call while a human waits.
3. **Authoritative tuples, seedable source.** CODEOWNERS improves the *seed*; the tuple remains the single answer to "who may consent," so the decision path never diverges from the ReBAC store.

## Costs accepted

Two more relations (`module_owner`, `maintainer`) to keep synced and to seed. A module with **no** `module_owner` tuple has nobody who can confirm an `mr → module` promotion: its conventions stay MR-scoped (staged, useful locally) until a steward is named — stated plainly, this is a gap that surfaces in staged-triage, not a silent drop.
