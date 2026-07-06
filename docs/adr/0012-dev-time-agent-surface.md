# ADR-0012 — A personal dev-time agent surface `surface:ide`

**Status:** accepted · **Context docs:** [01 §3](../01-concepts-and-scopes.md), [04 §1](../04-agent-interface.md), [05 §4](../05-policy.md), [07 §6](../07-operations.md)

## Decision

Personal dev-time agents (IDE- or CLI-hosted Claude / Codex — "ide" is the family name, CLI clients included) get **one new surface subtree `surface:ide`**, with per-session containers `devsession/<id>` (family `container`, parent `surface:ide`, `trust_class` `private` — a developer's local session is theirs). Dev-time activity does **not** get its own copy of project scopes: the flow context names the project (`project` field) and the touched paths, and chain resolution maps those onto the *existing* `project/...` + `module:*` scopes ([ADR-0009](0009-module-scope-family.md)) — the same scopes the GitLab surface uses. Cross-surface sharing stays pure scope placement ([ADR-0002](0002-single-scope-per-memory.md)).

**Ingestion and trust-model change, stated honestly.** There is no platform-side subscriber for a laptop — nothing observes a local session the way the Slack/GitLab ingestors observe those surfaces. So the local MCP client **registers episodes itself**, via a new MCP tool `memory_observe` — the **7th** tool; [doc 04 §1](../04-agent-interface.md)'s "six tools" count updates. Episodes from this path are **self-reported**: `source_kind` `dev_observation`, author = the on-behalf-of developer, and a **lower trust base** than platform-verified episodes ([doc 06 §3](../06-audit-privacy-security.md)). Safety rests on the layers that already exist: the same write pipeline (PII, policy), staged-by-default ([ADR-0003](0003-staged-tier-default.md)), and the [doc 05 §4.1](../05-policy.md) membership checks that re-validate real access at read time regardless of what the client claimed at write time.

## Alternatives considered

- **A separate `surface:cli` sibling**: two subtrees for one behavior with no policy difference between them — split for split's sake.
- **A platform-side ingestion proxy** for dev sessions: there is nothing to subscribe to; the proxy would just receive what the client sends, which is `memory_observe` with extra hops.
- **Reusing REST `POST /v1/episodes`, unauthenticated, for agents**: episode registration stays split by design — platform ingestion via REST, dev-time via the *policied* MCP tool, so an agent's writes always traverse the tool-permissioned, per-principal path.

## Why one `surface:ide` subtree wins here

1. **It reuses the whole model.** Placement, chains, promotion, policy layers all apply unchanged; only the episode *entry point* is new — because only the ingestion story is genuinely different (no subscriber).
2. **The trust question gets one honest answer.** Self-reported provenance is fenced by a lower trust base + staging + retrieval-time membership re-check, rather than pretending a client claim is platform-verified.

## Costs accepted

**The trust model changes, and this ADR names it rather than hiding it:** client-side episode registration means the *source is asserted by the client*, not observed by the platform — a compromised or lying client can register episodes attributing content to paths and an author it chooses. This is contained, not eliminated: the lower `dev_observation` trust base keeps such episodes below high-stakes retrieval floors, staging keeps them from steering behavior until independently reinforced, and read-time membership checks mean a false access claim buys nothing the on-behalf-of developer couldn't already see. The 7th tool also breaks the round "six" and adds versioned surface that ripples into deployed agent prompts ([ADR-0005](0005-standalone-service-mcp.md)). Rollout: this is **P5 — Dev-time & module scopes**, a new phase appended to [doc 07 §6](../07-operations.md) — never silently outrunning P4's stated exit criteria; its exit criterion is the module-scope scenario test.
