# ADR-0006 — Log reads-into-context in the audit log

**Status:** accepted · **Context docs:** [02 §5](../02-data-model.md), [06 §1](../06-audit-privacy-security.md), [07 §4](../07-operations.md)

## Decision

Every delivery of memories into an agent's context — ambient context blocks and deliberate `memory_recall` results — emits a `READ` event in `memory_events` (one event per delivery, carrying the delivered memory-id list, principal pair, flow context, and query/focus).

## Alternative considered

Write-only auditing (the industry norm — Mem0's history table, Zep's episodes, every product system): log how memories came to be, not who saw them. Far cheaper: reads outnumber writes by orders of magnitude, and READ events dominate event-log volume.

## Why logging reads wins here

1. **"Why did the bot say that?" is the audit question that actually gets asked.** Without read logs it is unanswerable — you can prove a memory existed, not that it entered the context that produced the answer. This closes the loop from an agent's output back to dana's original message (the worked scenario's step 6 is impossible without it).
2. **Reinforcement needs it anyway.** Useful-retrieval signals (doc 03 §4) and the useful-read-ratio quality metric (doc 07 §4) are derived from read events — the lifecycle design already pays for the data; the audit value rides along.
3. **Access-control verification**: read events are the evidence that retrieval-time checks behaved (or didn't) — the difference between claiming the source-visibility invariant and being able to demonstrate it for any past request.
4. **Leak forensics**: when a memory turns out to have been wrong, poisoned, or over-shared, the blast radius ("which sessions consumed it?") is a query.

## Costs accepted, and mitigations

Event volume dominated by reads: batched (one event per delivery, not per memory), monthly partitions, ids-not-content payloads, cold-archival after the retention window. Privacy consideration acknowledged: read logs are themselves behavioral data about users; they live under the same access controls and retention rules as the rest of the audit log, and are admin/audit-role readable only.
