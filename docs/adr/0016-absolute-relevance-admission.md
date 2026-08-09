# ADR-0016 — Admit retrieval candidates on absolute relevance, not on rank

**Status:** accepted · **Context docs:** [04 §3](../04-agent-interface.md), [04 §4](../04-agent-interface.md), [03 §5](../03-lifecycle.md)

## Decision

Every retrieval leg carries an **absolute admission gate**, applied in the leg's own query before fusion. The lexical leg admits only rows matching its `tsquery`; the literal leg only rows above a word-similarity threshold ([ADR-0017](0017-literal-retrieval-leg.md)); the vector leg only neighbours **within a cosine-distance ceiling** — being the nearest of a bad lot is not a qualification. Reciprocal-rank fusion then ranks whatever survived.

A query that admits nothing therefore retrieves nothing. The recency leg — candidates ordered by `last_accessed_at`, carrying no relevance signal at all — is reserved for **ambient recall without a focus** ([doc 04 §2.1](../04-agent-interface.md)), where there is no relevance question to answer. Deliberate recall, and ambient recall *with* a focus, return empty rather than falling back to it.

## Alternatives considered

1. **Rank-only, normalised to the best hit** (the prior behaviour): take top-K per leg, RRF-fuse, divide by the best fused value. Simple, scale-free, and the standard hybrid-search recipe.
2. **Threshold the fused score** instead of the legs — one knob rather than three.
3. **No gate at all**: return the top-K and let the agent judge, leaning on the prompt contract ([doc 04 §4](../04-agent-interface.md)).

## Why absolute admission wins here

1. **RRF ranks; it cannot say "no".** Normalising by the best fused value makes the top candidate perfectly relevant *by construction*. Ask a `#deploys` channel about parental leave and the block comes back full of deploy facts scored exactly like answers — the shape of a confident response with none of the content.
2. **Fabricated relevance defeats the conduct rules.** [Doc 04 §4](../04-agent-interface.md) tells agents to cite provenance and to say "I can't check my memory right now" instead of guessing, and [doc 07 §5](../07-operations.md) makes explicit unavailability a failure-mode requirement. A retrieval layer that can never return empty makes the honest answer unavailable to a compliant agent.
3. **Thresholds belong on legs, not on fusion.** A cosine distance, a `ts_rank_cd`, and a word similarity are each comparable to themselves and to nothing else; the fused RRF value has no units at all, so a cut on it is uninterpretable and drifts with K. Per-leg gates are also the only ones an index can enforce — the cut happens in Postgres, not over an over-fetched set, which is the same discipline [doc 04 §3](../04-agent-interface.md) already demands of access control.
4. **The two read paths want different empties.** A context block assembled at session start has no query; recency is the *intended* signal there, and continuity is the feature. Deliberate recall is a question, and "nothing on that" is a legitimate answer to a question — the one thing the old fallback could never say.

## Costs accepted

The distance ceiling is a per-deployment tuning knob calibrated against the embedding model, and set too low it drops real matches *silently* — the failure it prevents is loud, the failure it introduces is quiet. The shipped default is calibrated for the dev `hash` embedder's near-orthogonal geometry, exactly as the poisoning outlier threshold in [doc 06 §3](../06-audit-privacy-security.md) is, and a real model provider must retune it; because it is a property of the embedding space and not of an agent, it is a deployment setting rather than a policy weight ([ADR-0018](0018-retrieval-weights-are-policy.md)). Vector-leg recall becomes bounded by the model's absolute calibration rather than only by K. And empty results move work onto agent fallback behaviour — which is the point, but it makes the prompt contract's "don't guess" line load-bearing rather than decorative.
