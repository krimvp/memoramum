# AGENTS.md — guide for AI agents working on this repository

## What this repository is

Memoramum is a **design-documentation-only** repository: the architecture for a standalone
memory service for AI agents. There is **no implementation, no build, no tests, no
dependencies**. The deliverable is the prose. "Working on this repo" means reading,
writing, and keeping ~1,300 lines of tightly cross-referenced Markdown mutually consistent.

```
README.md                     entry point: pitch, worked scenario, doc map, GLOSSARY (normative)
index.html                    self-contained static page: high-level design overview
docs/
  01-concepts-and-scopes.md   core nouns; the scope tree; scope chains; principals
  02-data-model.md            reference Postgres DDL: scopes, memories, episodes,
                              provenance, memory_events — the enum source of truth
  03-lifecycle.md             status state machine; bi-temporal validity; reinforcement,
                              decay, promotion, forgetting
  04-agent-interface.md       MCP tool surface (6 tools); ambient vs deliberate recall;
                              retrieval scoring; the agent prompt contract; REST endpoints
  05-policy.md                learning policy (allow/stage/ask/deny), access policy (ReBAC
                              + attribute rules), layered evaluation
  06-audit-privacy-security.md audit guarantees, GDPR erasure pipeline, poisoning defenses,
                              PII pipeline
  07-operations.md            components, consolidator jobs, reference stack, SLOs, rollout
  appendix-prior-art.md       survey of Letta, Mem0, Zep/Graphiti, LangMem, etc.
  adr/000N-*.md               one-page records of the contested decisions (6 so far)
```

Read `README.md` first, then docs in numeric order — each ends with a "Continue with"
pointer to the next.

## Design invariants — do not contradict these

Every doc leans on these commitments. If an edit would violate one, that is not a wording
fix — it is a design change and needs an ADR (see below) and a sweep of every doc that
cites the old behavior.

1. **One memory, one scope.** Wider visibility = promotion to a broader scope, never
   multi-tagging. (ADR-0002)
2. **Supersede, don't overwrite.** Memory content is immutable; contradictions close the
   bi-temporal validity window and link a successor. Sole exception: `profile` kind.
   (ADR-0001)
3. **Staged by default.** Agent-inferred memories enter `staged` and earn `active` via
   reinforcement; `explicit_user_ask` skips staging. Staged input can never supersede
   active memories. (ADR-0003)
4. **Source-visibility invariant.** A memory never surfaces to a principal who could not
   see its source episodes, checked at retrieval time. Promotion is the only sanctioned
   relaxation.
5. **Policy is default-deny, strictest-wins.** Verdicts are `allow | stage | ask | deny`;
   layers compose org > surface > agent > user-preference, lower layers only tighten.
6. **Reads are events.** Every delivery of memories into an agent context emits a `READ`
   event. (ADR-0006)
7. **One Postgres.** Memories, episodes, events, scopes, and policy live in a single
   transactional store; revisit triggers are tabled in doc 07 §3. (ADR-0004)
8. **Standalone service, MCP facade.** No agent embeds the storage layer. (ADR-0005)

## Editing conventions

- **The glossary in `README.md` is normative.** Use its terms exactly (`staged`, not
  "pending"; `promotion`, not "escalation"; `scope chain`, `consolidator`, `episode`,
  `origin kind`, …). Introducing a new term means adding a glossary row in the same change.
- **Enums live in doc 02.** `kind`, `status`, `origin_kind`, `sensitivity`, `trust_class`,
  and the `memory_events.action` list are defined in the DDL of
  `docs/02-data-model.md`. Change them there first, then update every doc that enumerates
  them (03 uses statuses and origins heavily; 04 shows them in tool payloads; 05 matches
  on them).
- **The worked scenario threads through everything.** The 6-step Sage/Marge/dana story in
  `README.md` is retold in concepts (01 §5), lifecycle (03 §7), and policy (05 §6) terms.
  A new mechanism should be illustrated against it; a changed mechanism must keep all
  retellings consistent (dates, scopes `channel/C0DEP` / `workspace/T024B` / `mr/482`,
  event names).
- **Cross-reference style**: `[doc 03 §4](03-lifecycle.md)` from inside `docs/`,
  `[doc 03](docs/03-lifecycle.md)` from the README; ADRs as
  `[ADR-0002](adr/0002-single-scope-per-memory.md)`. Keep the trailing "Continue with…"
  chain intact when adding or renaming docs, and update the doc map table in `README.md`.
- **File naming**: docs are `NN-kebab-title.md` with stable numbers; ADRs are
  `NNNN-kebab-decision.md`, numbered sequentially.
- **Tone**: declarative, opinionated, compact. Tables over bullet sprawl. Prior-art
  attributions inline ("the Graphiti pattern", "Slack's invariant") with details reserved
  for the appendix. SQL/JSON blocks are specifications of shape, not contracts.

## ADRs

Any contested decision — one where a reasonable reviewer would ask "why not X?" — gets a
one-page ADR in `docs/adr/`, in the house format:

```
# ADR-NNNN — <imperative decision title>
**Status:** accepted · **Context docs:** [NN](../NN-….md), …
## Decision
## Alternative(s) considered
## Why <decision> wins here
## Costs accepted
```

The `/adr` skill scaffolds this. Docs cite the ADR at the point of decision; the ADR cites
the docs back.

## Validation

There is no build or test suite. Verification means:

- **Links**: a `PostToolUse` hook (`.claude/hooks/check-markdown-links.py`) checks every
  relative link and `#anchor` in a Markdown file you edit and reports breakage
  immediately — fix what it reports before committing.
- **Consistency**: run the `/docs-check` skill after substantive edits — it sweeps
  glossary-term usage, enum agreement with doc 02, scenario retellings, ADR
  cross-citations, and the "Continue with" chain.

## What NOT to do

- Do not add implementation code, package manifests, or CI for code — this repo stays
  docs-only until the design says otherwise (phased rollout is doc 07 §6).
- Do not "modernize" vocabulary or restructure docs wholesale; numbering and anchors are
  load-bearing (external references point at them).
- Do not soften the stated design stances (e.g. residual poisoning risk in doc 06 §3 is
  *deliberately* stated honestly — keep honest limitations honest).
- Do not resolve a documented trade-off differently in one doc without an ADR and a full
  sweep.
