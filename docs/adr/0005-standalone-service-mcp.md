# ADR-0005 — Standalone service with an MCP facade

**Status:** accepted · **Context docs:** [04](../04-agent-interface.md), [07 §1](../07-operations.md)

## Decision

Memoramum is a standalone network service. Agents talk to it through an **MCP server facade** (the tool surface of doc 04 §1); platform code, review UIs, and admin tooling use a REST/gRPC API over the same core. No agent embeds the storage layer directly.

## Alternative considered

An embeddable library sharing a common database (the LangMem/Letta-OSS shape): less infrastructure, no network hop on the hot path, each agent team ships at its own pace.

## Why a service wins here

1. **Policy and audit must be unbypassable.** With a library, every enforcement point (write pipeline, retrieval-time access checks, READ events) runs in the client's process and is one fork or one skipped upgrade away from being wrong — silently, in the exact subsystem whose job is to never be silently wrong. A service makes the API core the single choke point.
2. **Heterogeneous agents are the premise.** A Slack bot in TypeScript, an MR reviewer in Python, tomorrow's surface in whatever — a library binds the design to one runtime; MCP is already the lingua franca all of them speak.
3. **The background machinery is inherently service-shaped**: consolidator, sweeps, membership sync, erasure pipeline all need to run somewhere central regardless — the library option ends up as "a service *plus* N privileged clients."
4. **MCP specifically** (vs a bespoke SDK): tool definitions with rich descriptions double as agent documentation (the prompt contract travels with the tools); permissioning per tool is a solved pattern in agent harnesses.

## Costs accepted

A network hop on ambient recall (bounded by the context-block SLO, doc 07 §4) and a hard availability dependency (mitigated by the degrade-to-memoryless failure mode, doc 07 §5). The MCP facade must be versioned carefully — tool-shape changes ripple into deployed agent prompts.
