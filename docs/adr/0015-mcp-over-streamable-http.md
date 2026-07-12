# ADR-0015 — Serve the MCP facade over streamable HTTP for remote harnesses

**Status:** accepted · **Context docs:** [04 §1](../04-agent-interface.md), [04 §5](../04-agent-interface.md), [07 §1](../07-operations.md)

## Decision

The MCP facade gains a second transport: **streamable HTTP, served at `/mcp` on the same deployment as the REST facade**, authenticated with the principal-bound bearer tokens of [ADR-0014](0014-bearer-token-rest-auth.md). The token — not a caller-asserted header — names the `actor` of every tool call (an `agent:*` principal for personal harnesses); the flow context that a co-located launcher passes via environment variables ([ADR-0005](0005-standalone-service-mcp.md), [ADR-0012](0012-dev-time-agent-surface.md)) is instead asserted per-request via `X-Memoramum-*` headers (`On-Behalf-Of`, `Surface`, `Container`, `Participants`, `Session`, `Project`, `Touched-Paths`). stdio remains the transport for co-located surface integrations. Remote personal clients — an IDE- or CLI-hosted Claude Code or Codex on a laptop ([ADR-0012](0012-dev-time-agent-surface.md)) — connect with a URL and a token, and hold **no database credentials**; Postgres stays private to the deployment.

## Alternative(s) considered

- **Remote clients launch stdio `memoramum-mcp` against a network-exposed Postgres**: every laptop holds superuser-shaped database credentials, and a client that talks to the store directly can bypass the write pipeline, the retrieval-time access checks, and the `READ` events — precisely the "one fork away from silently wrong" failure [ADR-0005](0005-standalone-service-mcp.md) made the service exist to prevent. (This shape prompted this ADR.)
- **Agent verbs on the REST facade**: duplicates the seven-tool surface as a second API shape and loses what ADR-0005 chose MCP *for* — tool descriptions that double as the prompt contract, per-tool permissioning native to agent harnesses — while every harness grows bespoke client code instead of using its built-in MCP support.
- **SSH tunnel / VPN to the deployment, stdio unchanged**: secures the pipe but still distributes database credentials to every client and still lets clients around the service choke point; operationally it turns "issue a token" into "manage network access per laptop".

## Why streamable HTTP with bearer tokens wins here

1. **Unbypassable enforcement extends to remote clients.** Every remote tool call traverses the same API core as stdio and REST — policy, PII, retrieval-time access checks, and `READ` events cannot be skipped by construction ([ADR-0005](0005-standalone-service-mcp.md)).
2. **ADR-0014's seam is reused, not duplicated.** Token → actor is the same resolver on both facades; every logged `actor` stays a proven identity, and issue/revoke/rotate remain config changes.
3. **Header-asserted flow is trust already extended.** The stdio launch contract lets the launcher's environment assert the principal pair and flow; headers are the same assertion moved into the request, fenced the same way — the [doc 05 §4.1](../05-policy.md) intersection rule caps what any claimed flow can see at what the actor and on-behalf-of user could see at the source.
4. **It is native to the harnesses.** Claude Code and Codex both speak streamable HTTP MCP with static headers; connecting is configuration, not client code.

## Costs accepted

The HTTP facade runs **stateless** — one process serves many principals, so the per-process principal isolation of the stdio launch model no longer holds there; isolation rests entirely on the token → principal binding, which is why tokens are issued **per agent principal**, never shared. Flow context stays client-asserted (as `on_behalf_of` always was) — a lying client buys nothing the doc 05 §4.1 intersection rule wouldn't have shown it anyway, and dev-time writes stay fenced by the lower `dev_observation` trust base and staging ([ADR-0012](0012-dev-time-agent-surface.md)). Bearer credentials assume TLS at the deployment edge (stated, not solved, by [ADR-0014](0014-bearer-token-rest-auth.md)). And stateless request/response forgoes server-initiated MCP messages (sampling, notifications) — acceptable while the tool surface is strictly request-shaped.
