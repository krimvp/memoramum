# ADR-0002 — One scope per memory, promotion to share

**Status:** accepted · **Context docs:** [01 §3](../01-concepts-and-scopes.md), [03 §4](../03-lifecycle.md), [05 §3](../05-policy.md)

## Decision

Every memory lives in exactly one scope. Wider visibility is achieved by **promoting** the memory to a broader scope (an explicit, policied, audited operation that re-runs the write pipeline against the destination). Cross-surface sharing is scope placement in a scope both surfaces can read — not a sharing mechanism of its own.

## Alternative considered

Multi-scope tagging: a memory carries a set of scope tags; readers match on any tag. Flexible, avoids "which copy is canonical" questions, and lets one fact appear in several narrow scopes without living in a broad one.

## Why single placement wins here

1. **Access review must be tractable.** "What could leak into this channel's context?" must be answerable by walking a subtree, not by joining every memory's tag set against every scope's ACL. Security review of a tag lattice is the kind of complexity that quietly rots.
2. **The source-visibility invariant needs one answer** to "who may see this?" per memory. With tags, each tag independently relaxes visibility and the effective ACL is the union — the failure mode is silent over-sharing, precisely the accident this system exists to prevent.
3. **Sharing should be an event.** Making broadening an explicit promotion gives the audit trail a moment, an actor, a justification, and a policy decision to point at. With tags, sharing is an attribute edit.

## Costs accepted

A fact relevant to two *sibling* scopes (two unrelated channels) must either live at their common ancestor (workspace) or exist as two memories with shared `derived_from` — mild duplication the consolidator tolerates by design. Promotion adds a human step where tagging would have been instant; that friction is the point.
