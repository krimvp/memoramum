---
name: adr
description: Scaffold a new Architecture Decision Record in this repo's one-page house format. Use when a contested design decision needs recording, when a doc edit changes a previously-decided trade-off, or when the user asks to "write an ADR".
---

# Create an ADR

Arguments: a short description of the decision (e.g. `/adr use CRDTs for profile merges`).
If no argument is given, infer the decision from the current conversation; if still
ambiguous, ask.

## Steps

1. **Number it.** `ls docs/adr/` and take the next sequential four-digit number.
2. **Name it.** `docs/adr/NNNN-<kebab-case-imperative-decision>.md` — the title states the
   decision, not the topic (`0001-supersede-dont-overwrite.md`, not `0001-updates.md`).
3. **Write it** in exactly the house format (read an existing ADR first to match voice):

   ```markdown
   # ADR-NNNN — <Imperative decision title>

   **Status:** accepted · **Context docs:** [NN](../NN-….md), [NN §M](../NN-….md)

   ## Decision

   One tight paragraph stating what is decided, including the exception(s) if any.

   ## Alternative(s) considered

   Each real alternative, named after the system that embodies it where possible
   ("Mem0-style in-place mutation"), with its genuine advantages stated fairly.

   ## Why <decision> wins here

   2–4 numbered arguments. "Here" matters: arguments should tie back to this design's
   core requirements (auditability, scoped access, governance, erasure), not be
   universal claims.

   ## Costs accepted

   Honest paragraph of what this decision makes worse and how (or whether) it is
   mitigated, with doc references.
   ```

4. **Keep it one page** (~20–30 lines). If it wants to be longer, the detail belongs in a
   numbered doc that the ADR references.
5. **Cross-link both directions**: cite the new ADR from the doc section(s) where the
   decision bites (`[ADR-NNNN](adr/NNNN-….md)`), and list those docs in the ADR's
   **Context docs** line. If the decision touches the reference stack, also check the
   revisit-triggers table in `docs/07-operations.md` §3.
6. **Glossary check**: if the decision introduces a new term, add it to the README
   glossary in the same change.
