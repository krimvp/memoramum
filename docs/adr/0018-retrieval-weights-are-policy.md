# ADR-0018 — Make retrieval weights policy, and make them exponents

**Status:** accepted · **Context docs:** [04 §3](../04-agent-interface.md), [05 §1](../05-policy.md), [05 §4.2](../05-policy.md)

## Decision

The five factors of the [doc 04 §3](../04-agent-interface.md) composite are tunable per agent through a `read.weights` block in a policy document, and each weight is an **exponent** on its factor, defaulting to `1.0`:

```
score(m) = relevance(m)^w_rel · R(m)^w_ret · trust_score(m)^w_trust
                              · status_weight(m)^w_status · scope_proximity(m)^w_prox
```

`0` disables a factor, `1` is the shipped behaviour, `> 1` sharpens it. Weights are bounded to `[0, 5]` at policy-parse time.

Composition departs from the strictest-wins rule that governs the four read *gates*: **the org layer's weights are final**, and below org the **narrowest layer that sets a weight wins** (agent over surface over the deployment default). Unset keys fall through.

## Alternatives considered

1. **Leave them global settings** — the status quo, and the state doc 04 §3 already described as policy-tunable while the implementation carried a comment admitting they were not.
2. **Multiplicative coefficients**, which is what the doc's `w_rel · relevance(m)` notation literally says.
3. **Strictest-wins across layers**, like every other axis in [doc 05 §2](../05-policy.md).

## Why exponents, decided by the narrowest layer, win here

1. **A coefficient on a shared factor is a no-op.** Every candidate in a result set is scored by the same formula, so multiplying one factor by a constant rescales every score identically and reorders nothing: `w_rel = 0.5` halves the numbers and changes the answer not at all. That is precisely why the reference implementation never grew a `w_rel` — the notation promised a knob that cannot exist in that form. An exponent bends the factor's *curve*, which is what "weighting" has to mean here.
2. **The tuning requests are real and shaped like exponents.** A high-stakes agent wants staged items buried without being excluded (`status: 2.0`, distinct from `include_staged: false`). An agent whose chain is one scope deep wants the proximity penalty gone (`scope_proximity: 0`). An incident-response surface wants recency to dominate (`retention: 2.0`); an agent recalling org invariants wants it not to matter (`retention: 0`).
3. **"Strictest" is undefined for a weight.** Is `scope_proximity: 2.0` stricter than `0.5`? Neither shrinks what a principal may see. Confidentiality is the gates' job — `trust_floor`, `sensitivity_ceiling`, `include_staged`, `deny_categories` keep strictest-wins and keep composing monotonically. Weights only reorder what the gates already permitted, so they need a *decisive* rule rather than a monotone one, and the narrowest layer is the one that knows the agent's job.
4. **The org floor still holds where it matters.** Principle 3 of [doc 05](../05-policy.md) — the org layer is un-overridable — survives verbatim: an org that cares about a weight sets it and no lower layer can move it. What lower layers gain is the freedom the org declined to spend.

## Costs accepted

Two composition rules now live in one document: gates tighten downward, weights are decided by the narrowest setter. A reviewer reading a policy stack has to know which keys are which, so [doc 05 §4.2](../05-policy.md) states the split explicitly rather than leaving it to be inferred. A pinned org weight cannot be relaxed by a surface with a good reason — the same bluntness the org floor has everywhere else. Ranking becomes deployment-specific: "why did this memory rank third?" is no longer answerable from the formula alone, and needs the policy versions in force at that moment — which the versioned documents of [doc 05 §5](../05-policy.md) do provide, but as a second lookup. And a misconfigured weight degrades relevance quietly, with no verdict and no event to point at; simulation mode ([doc 05 §5](../05-policy.md)) covers write decisions, not ranking.
