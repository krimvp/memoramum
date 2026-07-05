# ADR-0001 — Supersede, don't overwrite

**Status:** accepted · **Context docs:** [02](../02-data-model.md), [03](../03-lifecycle.md)

## Decision

Memory content is immutable. Corrections, updates, and contradictions create a **successor** memory and close the predecessor's bi-temporal validity window (`invalid_at`, `superseded_by`, status `deprecated`). The four temporal fields (`valid_at`, `invalid_at`, `recorded_at`, supersession time) are first-class on every memory. Sole exception: `profile`-kind documents update in place, with every edit in the event log.

## Alternative considered

Mem0-style in-place mutation: an LLM update phase emits `ADD/UPDATE/DELETE/NOOP` against similar existing memories, editing or deleting rows directly. Simpler storage, always-current working set, no deprecated rows to filter.

## Why supersession wins here

1. **Auditability is a core requirement.** In-place UPDATE destroys the very record the audit trail must explain; reconstructing "what did we believe in April" from a mutating table requires the event log to carry full before/after content — at which point you have supersession with worse ergonomics.
2. **An LLM judgment should not be able to destroy data.** The update decision is a model output; model outputs are sometimes wrong and sometimes adversarially induced (poisoning). Supersession makes every such decision reversible and reviewable.
3. **Temporal questions are real questions** in an org context ("when did the deploy policy change?"). Zep/Graphiti demonstrated bi-temporal windows are cheap to carry and exactly answer them.

## Costs accepted

Deprecated rows accumulate (mitigated by archival sweeps, doc 03 §5); current-state queries carry a status filter (indexed); dedupe/merge becomes a consolidator responsibility rather than a write-path side effect.
