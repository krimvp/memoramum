# ADR-0017 — Let the connect URL carry the flow context, not only headers

**Status:** accepted · **Context docs:** [04](../04-agent-interface.md), [07 §1](../07-operations.md)

## Decision

The remote MCP endpoint ([ADR-0015](0015-remote-mcp-streamable-http.md)) reads the **flow context from the connect URL's query string** when the per-request `X-Memoramum-*` header is absent: `surface`, `container`, `participants`, `session`, `project`, `paths`, mirroring the header suffixes, plus `on_behalf_of` where the credential has not proven it ([ADR-0016](0016-oauth-resource-server.md)) and `actor` in the dev-mode shim. A header always wins over the URL — it is stated per call, the URL once at connect time.

```
https://memory.acme.example/mcp?surface=ide&project=project/platform-api
```

Nothing about trust changes: both carriers are *asserted* by whoever configured the client, the same trust stdio extends to the launching integration's environment variables, and the [doc 05 §4.1](../05-policy.md) intersection rule caps what the assertion can reach. Anything the credential proved — the actor, and an OAuth-authorized `on_behalf_of` — still cannot be overridden by either carrier. The OAuth resource identifier is the URL *without* query (RFC 8707 canonicalization), so a flow-carrying connect URL authenticates against the same resource and the same protected-resource metadata.

## Alternative(s) considered

- **Headers only** (the [ADR-0015](0015-remote-mcp-streamable-http.md) status quo): one carrier, nothing to keep in parity; but the clients this facade exists to serve are configured with a URL, and support for custom static headers is uneven across them. Connected mode would then either stay the preserve of bespoke harnesses — most of what ADR-0015 set out to fix — or lose the flow entirely, and a flowless call is not a degraded call so much as a different one: writes are refused outright (there is no source scope to route to, [doc 01 §3.3](../01-concepts-and-scopes.md)), while reads *widen*, since with no container and no surface the chain falls back to every non-private container the agent is directly enrolled in, losing the container's own scope and the proximity ordering that ranks near scopes first.
- **A `flow` argument on every tool**: proven per call, no transport coupling; but it hands the context to the model, re-opening "agents never name raw scope ids" ([doc 04](../04-agent-interface.md)) — the service decides what a flow makes visible precisely so an agent cannot widen it — and it grows the signature of seven tools.
- **An initialize-time handshake carrying the flow**: idiomatic MCP for a stateful server; but this endpoint is stateless so any replica serves any call (ADR-0015), so per-session state would have to be re-derived per request regardless.
- **A short-lived connection token encoding the flow**: the flow would finally be *proven*; but issuance explodes per agent × container — the reason ADR-0015 rejected baking flow into the credential — and a signed blob is not something a human pastes into a client config.

## Why the connect URL wins here

1. **It is the one channel every MCP client has.** A URL is the whole configuration surface of "add this server"; making it sufficient is what turns a deployment into something a developer can connect to in one command.
2. **Same trust, so no new argument to have.** The URL asserts exactly what the header asserted, bounded by exactly the same intersection rule — this is an ergonomics decision, not a security one, and it is worth recording only because ADR-0015 named headers as *the* carrier.
3. **Headers stay authoritative**, so harnesses that can state a live per-call flow (an IDE integration moving between projects, an MR bot naming touched paths) lose nothing and need no migration.
4. **The static/dynamic split matches the data.** Surface and project are properties of a connection; touched paths and participants change per call. Each carrier now fits the field it is good at.

## Costs accepted

A connect URL is long-lived and copy-pasted, so a stale one keeps asserting a stale container until a human edits it — the failure is quiet (memories route to the wrong container), and the mitigation is that per-call fields, `paths` above all, belong in headers. Query strings turn up in proxy and access logs: flow fields are scope ids and participant principals, not secrets, but they are now more exposed than a header would be — deployments that consider participant lists sensitive should keep them in headers. Two carriers must stay in parity, which one resolver keeps cheap. And a URL-asserted `actor` in dev mode is exactly as weak as the header it mirrors, so the loopback-only rule on the shim stands unchanged.
