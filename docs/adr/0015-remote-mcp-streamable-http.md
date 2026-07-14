# ADR-0015 — Serve the MCP facade remotely over streamable HTTP

**Status:** accepted · **Context docs:** [04](../04-agent-interface.md), [07 §1](../07-operations.md)

## Decision

The MCP facade runs the one doc 04 §1 tool registry over **two transports**: **stdio** — one process per session context, the principal pair and flow injected by the launching surface integration's environment ([ADR-0005](0005-standalone-service-mcp.md), [ADR-0012](0012-dev-time-agent-surface.md)) — and **streamable HTTP**, served centrally beside the REST facade, so harnesses connect to the deployed service instead of spawning anything near the agent. Remote callers authenticate with the **same principal-bound bearer tokens as REST** ([ADR-0014](0014-bearer-token-rest-auth.md); one `MEMORAMUM_API_TOKENS` for both facades): the token names the actor, refused at the door (HTTP 401) before any JSON-RPC processing; `on_behalf_of` and the flow context ride per-request `X-Memoramum-*` headers set by the connecting harness — the same trust stdio extends to the launcher's environment variables. The endpoint is stateless: any replica serves any call. With no tokens configured, the dev-mode header shim applies and the CLI refuses to bind beyond loopback, exactly as REST. Either way, **MCP is the only agent-facing surface** — REST stays platform/admin (doc 04 §5).

## Alternative(s) considered

- **stdio only (status quo)**: no second transport to keep in parity; but whatever spawns the process near the agent must hold store credentials, so a central deployment degenerates into "a service plus N privileged sidecars" — the exact shape ADR-0005 rejected.
- **Remote agents use the REST facade**: one remote surface already exists; but it duplicates the verb set, forfeits tool-descriptions-as-documentation and per-tool permissioning (ADR-0005's reason for MCP), and ends the single agent-facing layer.
- **Flow baked into the credential** (a token per agent×container): the flow would be proven, not asserted; but flow varies per call while credentials are per principal — issuance explodes, and `on_behalf_of` would still need asserting.
- **The legacy HTTP+SSE transport**: wider support in older clients; but deprecated by the MCP spec in favor of streamable HTTP, and its long-lived session streams fight stateless replicas.

## Why streamable HTTP with shared tokens wins here

1. **The choke point survives distribution.** Policy, audit, and READ events stay unbypassable (ADR-0005) only if remote agents reach the API core through a facade — never through credentials shipped to their side of the network.
2. **The audit invariant extends unchanged**: the ADR-0014 seam (bearer credential → proven actor) is reused verbatim, so every MCP-originated event names an actor somebody proved, with one issuance/rotation story for both facades.
3. **Nothing new is trusted.** The harness that sets `X-Memoramum-*` headers plays the same role as the integration that sets `MEMORAMUM_*` env vars; the doc 05 §4.1 intersection rule caps both identically.
4. **Statelessness matches the premise**: harnesses connect from anywhere, reconnect freely, and no replica holds session state the next request needs.

## Costs accepted

The flow remains asserted rather than proven — as everywhere ([ADR-0014](0014-bearer-token-rest-auth.md) costs), fenced by the [doc 05 §4.1](../05-policy.md) intersection rule. Every tool call re-resolves principal and flow from headers (a header parse; noise against the store round-trip it precedes). Two transports must stay in parity — one shared tool registry makes drift a code-review smell, not impossible. And header-borne context ties the remote facade to HTTP; a non-HTTP facade would need its own context carrier. DNS-rebinding protection is host-allowlist-by-config in token mode (`MEMORAMUM_MCP_ALLOWED_HOSTS`) — a browser cannot attach the bearer token, so the residual exposure is the dev-mode shim, which stays loopback-only with the SDK's localhost allowlist.
