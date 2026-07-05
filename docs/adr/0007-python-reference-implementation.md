# ADR-0007 — Implement the reference service in Python, raw SQL, thin facades

**Status:** accepted · **Context docs:** [02](../02-data-model.md), [04](../04-agent-interface.md), [07 §6](../07-operations.md)

## Decision

The reference implementation (started at rollout phase P1, `src/memoramum/`) is Python 3.11+:
psycopg 3 running raw SQL against the doc 02 DDL — the migrations *are* that DDL, verbatim —
with FastAPI as the REST facade and the official MCP Python SDK as the agent facade. No ORM,
no service framework beyond the facades; one `MemoryService` class is the API core and single
enforcement point (doc 07 §1). Tests are integration tests against a real Postgres + pgvector.
Implementation lands strictly by rollout phase (doc 07 §6): the schema carries every field from
day one, code populates them phase by phase.

## Alternatives considered

- **TypeScript/Node**: the MCP SDK is equally first-class and the facades would look the same;
  but phases P2–P4 are extraction workers, consolidator LLM jobs, and PII classifiers — LLM
  tooling that the Python ecosystem serves better, and a split codebase (TS service, Python
  workers) is worse than either alone.
- **Go**: the best language for a boring transactional service; but the same P2–P4 argument
  applies with more force, and the service's hot path is IO + SQL where Go's advantages buy
  little against the doc 07 §4 SLOs.
- **SQLAlchemy/ORM + generated migrations**: friendlier query composition; but doc 02 is the
  normative schema, and an ORM model would restate it in a second dialect that can drift. Raw
  SQL keeps the DDL literally executable and the diff between spec and store empty.

## Why Python + raw SQL wins here

1. **The docs are the contract.** The migration files are copied from doc 02; a schema change
   is a doc change first, mechanically. The enum lists, the event actions, the verdict set all
   exist in exactly one prose home and one executable home.
2. **The expensive phases are LLM-shaped.** Extraction, consolidation, contradiction judging,
   classification (P2–P4) all call models; Python is where that tooling matures first.
3. **Small surface, few dependencies.** Six tools, a dozen REST routes, one store: the whole
   P1 service is a few files a reviewer can hold in their head — matching the design's own
   "deliberately boring" stance (doc 05 §2).

## Costs accepted

Python's throughput ceiling caps the service well below what Go would allow (acceptable
against doc 07 §4 targets; revisit alongside the doc 07 §3 triggers); no compile-time types —
mitigated by the small surface and integration tests against real Postgres; the venv/packaging
operational tax.
