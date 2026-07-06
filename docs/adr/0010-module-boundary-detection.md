# ADR-0010 — Module boundaries come from explicit path-glob config

**Status:** accepted · **Context docs:** [01 §3](../01-concepts-and-scopes.md), [02 §1](../02-data-model.md), [05 §1](../05-policy.md)

## Decision

A flow's module scopes ([ADR-0009](0009-module-scope-family.md)) are resolved from an explicit, **admin-maintained path-glob mapping**: a `module_paths` operational table (`module_scope_id`, `glob`, longest / most-specific match wins) mapping repository paths to module scopes. The service reads "modules touched" from the file paths already in the flow context — MR diff paths, dev-session touched files — and inserts the matching module scopes into the chain. The same resolution feeds **write-time tagging** (extraction records which modules a candidate's source paths touch) and **read-time chain inclusion** ([doc 01 §3.3](../01-concepts-and-scopes.md)). Paths that match no glob belong to no module: they resolve to the project scope, which is already in the chain. **Fail-open to project, never to a wrong module.**

## Alternatives considered

- **CODEOWNERS-derived boundaries**: infer modules from the repo's `CODEOWNERS` file. But it may not exist; ownership boundaries are not convention boundaries (who reviews `payments/` ≠ where payments conventions live); and it makes us parse and depend on another tool's semantics.
- **Directory-convention** (top-level dirs, or `packages/*`, are modules): zero config, but breaks on every non-standard layout and is implicit magic — a path silently becomes a scope with no reviewable record of the decision.

CODEOWNERS may *seed* the config where it exists; humans own the result.

## Why explicit globs win here

1. **A scope's boundary should be a reviewable artifact**, not an inference. The mapping is a table an admin diffs, the same posture as every other operational-config surface ([ADR-0004](0004-postgres-reference-stack.md)).
2. **Decoupled from ownership.** Conventions and review-ownership drift independently; binding scope boundaries to CODEOWNERS would couple them and inherit its gaps.
3. **The failure mode is safe by construction.** An unmatched or mis-globbed path lands at the broader-but-correct project scope — never at a *different* module's scope, which is the only genuinely dangerous outcome (a payments convention surfacing as billing's).

## Costs accepted

The mapping needs maintenance: a new directory added to the repo isn't a module until someone globs it. A stale glob quietly routes new paths to the project scope rather than the intended module — a *visibility* miss (the convention sits broader than ideal, or waits in staged-triage for a promotion that never gets proposed), surfaced in the staged-triage view, not a correctness or leak failure.
