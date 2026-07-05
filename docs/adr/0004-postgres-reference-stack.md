# ADR-0004 — One Postgres; ReBAC-compatible tables instead of a policy engine

**Status:** accepted · **Context docs:** [02](../02-data-model.md), [05 §4](../05-policy.md), [07 §3](../07-operations.md)

## Decision

The reference stack is a single Postgres (16+, pgvector) holding memories, episodes, events, scopes, and policy. Scope-membership ReBAC is plain relation-tuple tables evaluated in-service, with the schema kept isomorphic to SpiceDB/OpenFGA models. Retrieval is hybrid pgvector HNSW + tsvector. No dedicated vector DB, graph DB, or authorization engine at launch.

## Alternatives considered

- **Dedicated vector store (Qdrant/Pinecone)**: better ANN performance at scale; but adds a second store whose contents must be kept consistent with the system of record, and whose deletes must be verified separately for erasure (the ghost-vector problem twice).
- **Temporal knowledge graph (Graphiti/Neo4j)**: strictly more expressive retrieval; but heavy LLM-driven entity-resolution machinery, a second query model, and per-fact ACLs remain unsolved there anyway.
- **SpiceDB/OpenFGA now**: real Zanzibar semantics (userset rewrites, consistency tokens); but a separate service to run, and single-org scope math (subtree walk + membership check) doesn't need it.
- **OPA as the primary policy engine**: general; but policy-as-Rego is harder to review than declarative strategy YAML, and most rules here are structural set-membership.

## Why one Postgres wins here

1. **Atomicity where it counts**: a memory + its provenance + its policy decision + its event commit in one transaction. Split stores turn the audit guarantee into an eventual-consistency apology.
2. **Erasure is hard enough in one store.** Every additional copy of content/embeddings is another place GDPR deletion can silently fail.
3. **Single-org scale fits**: low-millions of atomic memories and sub-thousand-QPS reads are comfortable pgvector/tsvector territory; the p95 targets (doc 07 §4) are achievable without exotic infra.
4. **Reversibility is designed in**: the revisit-trigger table (doc 07 §3) names the exact conditions for adopting each alternative, and the ReBAC tables/episode-provenance shapes make those moves additive (data export / added layer), not redesigns.

## Costs accepted

pgvector HNSW tuning and index-rebuild operations become our problem; RLS + in-service authz means the service is the enforcement point (no independent authz service to attest); if multi-tenancy arrives, parts of this ADR are explicitly superseded.
