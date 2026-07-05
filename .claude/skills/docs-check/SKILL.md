---
name: docs-check
description: Consistency sweep for the Memoramum design docs — cross-references, enum agreement with the doc 02 DDL, glossary-term usage, worked-scenario retellings, ADR citations, and the reading chain. Use after any substantive doc edit, before committing, or when the user asks to check/validate/lint the docs.
---

# Docs consistency check

This repo has no build; this sweep is the test suite. Run every section, collect findings,
then report them as a single prioritized list (broken links and enum drift first, style
last). Fix findings only if the user asked for a fix pass; otherwise report.

## 1. Links and anchors

For every `*.md` file, check that relative links resolve and `#anchors` match a real
heading in the target (GitHub slug rules). The hook script can do this in one pass:

```bash
for f in README.md AGENTS.md docs/*.md docs/adr/*.md; do
  CLAUDE_HOOK_CHECK_FILE="$f" .claude/hooks/check-markdown-links.py || true
done
```

## 2. Enum agreement with doc 02

`docs/02-data-model.md` DDL is the source of truth for: `kind`, `status`, `origin_kind`,
`sensitivity`, `trust_class`, `memory_events.action`, scope `family`. Grep each enum
value across `README.md` and `docs/` and confirm no doc uses a value missing from the
DDL, and no DDL value is undocumented in docs 01/03/05 where it belongs.

## 3. Glossary discipline

Every term in the README glossary table must be used with that exact meaning wherever it
appears. Spot-check the load-bearing ones: `staged`, `promotion` (status vs scope — doc 03
§4 distinguishes them), `episode`, `origin kind`, `scope chain`, `consolidator`,
`invariant`, `tombstone`, `reinforcement`, `ambient recall` / `deliberate recall`.
Flag near-synonyms that crept in ("pending", "escalate", "pinned" outside the invariant
definition, "log entry" for event).

## 4. The worked scenario

The 6-step scenario appears in `README.md`, `docs/01-…` §5, `docs/03-…` §7 (with dates),
and `docs/05-…` §6. Verify all retellings agree on: actors (Sage, Marge, dana), scope ids
(`channel/C0DEP`, `workspace/T024B`, `mr/482`, `org:acme`), event names
(PROPOSE/REINFORCE/PROMOTE_STATUS/PROMOTE_SCOPE/CONFIRM/READ/SUPERSEDE), statuses, and
the date timeline in doc 03 §7.

## 5. Structure

- Doc map table in `README.md` lists every file in `docs/` (and nothing that doesn't exist).
- Each numbered doc ends with a "Continue with [doc NN…]" pointer forming an unbroken
  01→…→07 chain (07 closes with appendix + ADRs).
- Every ADR is cited from at least one doc, and every ADR's **Context docs** line points
  back at docs that actually cite it.
- `index.html` claims nothing the docs contradict (skim its section headings against the
  doc map — it is a summary, not normative).

## 6. Report format

```
CRITICAL  (broken links, enum drift, scenario contradictions)
WARN      (glossary drift, missing cross-citations, stale doc map)
STYLE     (tone/format deviations from AGENTS.md conventions)
```

Each finding: file:line, what's wrong, the one-line fix.
