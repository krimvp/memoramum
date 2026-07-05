# ADR-0008 — Compute operational metrics from the store, not from process instrumentation

**Status:** accepted · **Context docs:** [04 §5](../04-agent-interface.md), [07 §4](../07-operations.md)

## Decision

The doc 07 §4 "metrics that matter" are SQL aggregates over the tables that already exist — `memory_events`, `memories`, the review queues, `erasure_requests` — computed on demand and served as one snapshot at `GET /v1/metrics` (admin/auditor only). There is no second instrumentation pipeline: no in-process counters for these metrics, no metrics store. Standard service health (latency histograms, error rates, saturation) stays with whatever APM the deployment already runs — this decision covers the memory-quality metrics only, which no APM can see.

## Alternative considered

Prometheus-style instrumentation: counters and histograms incremented on the hot path, scraped from a `/metrics` exposition endpoint, aggregated in a time-series store. The industry default, and genuinely better at high-cardinality time-series (per-second rates, alerting on derivatives) and at metrics with no table behind them.

## Why store-derived wins here

1. **The event log already is the instrumentation.** Reads are events ([ADR-0006](0006-log-reads.md)), policy decisions are events, and everything commits transactionally with the write ([ADR-0004](0004-postgres-reference-stack.md)) — a counter would be a lossy second copy of rows we are contractually keeping anyway. ADR-0006 explicitly priced this in: "the lifecycle design already pays for the data."
2. **Auditability extends to the numbers.** A dashboard figure an admin acts on (a `deny` spike, staged-precision collapse) can be exploded into the exact events behind it with the same query vocabulary as `GET /v1/audit/events` — counters can only be believed, aggregates can be replayed.
3. **Counters can't answer these questions.** Useful-read ratio and staged-tier precision are *cohort* metrics — "of the memories read, which were later reinforced?" — joins over history, not monotonic increments. Instrumenting them would mean re-implementing the join incrementally, badly.
4. **Zero hot-path cost.** Snapshot queries run at read time on indexed columns (`action, at`); the write pipeline is untouched.

## Costs accepted

No native time-series: the endpoint is a point-in-time snapshot over a window, so trend lines require an external scraper polling it (which is also the escape hatch — a Prometheus exporter over `GET /v1/metrics` needs no schema change). Snapshot cost grows with event volume; the ADR-0006 mitigations (monthly partitions, cold-archival) bound the scanned window, and a sustained-cost trigger belongs in the doc 07 §3 revisit table alongside its neighbors. Event-log lag is reported but structurally zero in this stack — the field exists so the shape survives a deployment that batches event writes.
