# ADR-0014 — Authenticate REST callers with principal-bound bearer tokens

**Status:** accepted · **Context docs:** [04 §5](../04-agent-interface.md), [06 §1](../06-audit-privacy-security.md), [07 §1](../07-operations.md)

## Decision

REST callers authenticate with **static bearer tokens, each bound to exactly one principal**, injected as deployment configuration (`MEMORAMUM_API_TOKENS`, comma-separated `principal=token` entries). The token — not a caller-asserted header — names the `actor` of every call: a missing or unknown token is `401`, and a request that asserts a different actor (header, or the `/v1/context-block` body principal) than the one its token is bound to is `403`. `on_behalf_of` stays caller-asserted — delegation is the same trust the design already extends to the surface integration that launches the MCP server with the principal pair in its environment ([ADR-0005](0005-standalone-service-mcp.md), [ADR-0012](0012-dev-time-agent-surface.md)). Exceptions: `/healthz` is unauthenticated, and with **no tokens configured** the facade runs the header-asserted dev-mode shim — which the CLI entry point refuses to expose beyond loopback.

## Alternative(s) considered

- **One shared secret, principals still header-asserted**: the least configuration; but every credential holder can act as any principal — including `user:root` policy administration and the auditor's event log — and the audit trail records identities nobody proved. It authenticates the *deployment's friends*, not a principal.
- **OIDC/JWT against an identity provider**: real key rotation, expiry, and federation — the right shape if multi-tenancy arrives ([doc 07 §3](../07-operations.md) revisit triggers); but a full IdP dependency for a single-org reference implementation, and it replaces exactly the same seam (token → principal resolution) this decision isolates.
- **mTLS at the edge**: strong transport identity, no secrets in headers; but it names a *certificate holder* to the proxy, not a principal to the service — the service would still need a mapping layer, which is this ADR with heavier machinery.

## Why principal-bound bearer tokens win here

1. **The audit invariant keeps its meaning.** Reads are events naming their actor ([ADR-0006](0006-log-reads.md)); an event log where actors are self-asserted by anyone holding a shared secret audits nothing. Binding token → principal makes every logged `actor` a proven identity.
2. **Default-deny extends to the front door.** Policy composes on principals (doc 05); authentication that doesn't establish a principal leaves the strictest-wins evaluation resting on an honor system.
3. **It is the smallest credential that does this.** Issue, revoke, and rotate are config changes; no new service, no clock-skew or key-distribution machinery — proportional to a single-org reference stack ([ADR-0004](0004-postgres-reference-stack.md)).
4. **The seam is the upgrade path.** The resolver (bearer credential → actor) is one dependency; an IdP later replaces the lookup, not the handlers.

## Costs accepted

Static tokens have no expiry and rotate only by config change and restart — acceptable at reference-implementation scale, and the first thing an IdP replaces. A platform component that bootstraps context blocks for several agents holds one token per agent principal it speaks for, since impersonation is deliberately not granted to any token. Bearer credentials assume TLS — termination is the deployment edge's concern, stated here rather than solved here. And `on_behalf_of` remains asserted rather than proven: a token bound to `agent:sage` can claim to act for any user, fenced — as everywhere else — by the [doc 05 §4.1](../05-policy.md) intersection rule, which caps what the claim can see at what that user could see.
