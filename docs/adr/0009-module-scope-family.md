# ADR-0009 — Add `module` as a parallel scope family

**Status:** accepted · **Context docs:** [01 §3](../01-concepts-and-scopes.md), [02 §1](../02-data-model.md), [05 §3](../05-policy.md)

## Decision

Monorepo-module conventions get a new **parallel scope family** `module`, alongside `subject` and `agent` — not a new tier in the container tree. Family value `module`; scope ids `module:<project>/<module-path>` (e.g. `module:platform-api/payments`); `parent_scope_id` is the project container scope (`project/platform-api`). Module scopes are pulled into a read's scope chain **contextually** — "the modules touched by this MR or dev-session" — exactly the way participant subject scopes are pulled in today ([doc 01 §3.3](../01-concepts-and-scopes.md)). DDL change: the `scopes.family` CHECK gains `'module'` ([doc 02 §1](../02-data-model.md) + migration).

## Alternative considered

Nest `module` as a strict tree ancestor of `mr/*` — the MR scope's parent becomes its module, the module's parent the project. Modules would then be ordinary container scopes and need no new family.

## Why a parallel family wins here

1. **A scope has exactly one parent; an MR touches many modules.** The nesting model forces a single module-parent per MR, which is false — a diff spans `payments/` and `billing/` routinely. Making the MR a child of several modules is a DAG, and [ADR-0002](0002-single-scope-per-memory.md) buys its tractability guarantees precisely from a clean tree.
2. **Access review stays a subtree walk.** With the project scope as parent, module scopes inherit project enrollment and membership: `ancestors()` / `readable()` ([doc 05 §4](../05-policy.md)) resolve module access with no special case — the same guarantee ADR-0002 protects.
3. **Contextual chain inclusion already exists.** Subject scopes are inserted by "who is present in the flow"; module scopes are inserted by "what paths the flow touched" ([ADR-0010](0010-module-boundary-detection.md)). One mechanism, two selectors — no new chain machinery.

## Costs accepted

Chain resolution now needs a boundary-detection step to answer "which modules did this flow touch" ([ADR-0010](0010-module-boundary-detection.md)) — a module scope is only useful once paths map to it. A convention that genuinely spans several sibling modules has no single home: it lives at the project scope, or duplicates across module scopes with shared `derived_from` — the same mild sibling-duplication ADR-0002 already tolerates by design.
