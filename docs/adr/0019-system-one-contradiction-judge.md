# ADR-0019 — Use a System One choice question as the contradiction judge

**Status:** accepted · **Context docs:** [03 §3](../03-lifecycle.md), [07 §3](../07-operations.md), [07 §5](../07-operations.md)

## Decision

The model behind the contradiction judge seam ([doc 03 §3](../03-lifecycle.md)) is a **System One** model: it takes the pair `{old_memory, new_memory}` as state and answers one `choice` question — `duplicate` / `contradiction` / `unrelated`, each criterion written in doc 03's terms — with a choice, a confidence and per-option probabilities. The judge counts a `contradiction` or a `duplicate` only at or above a **fixed confidence gate of 0.8**; below it, and on any fault (no key at startup, HTTP error, timeout, non-JSON, schema mismatch), the verdict is `unrelated` and the write continues exactly as with the deterministic `exact` judge. The probabilities, confidence, model id and token usage of every counted verdict are recorded on the `PROPOSE` event. The judge is selected with `MEMORAMUM_JUDGE=jev`; the deterministic dev judges stay the default.

## Alternative(s) considered

1. **An LLM judge that writes a verdict** (the Graphiti / Mem0 pattern: a generative model reads both facts and emits `ADD` / `UPDATE` / `NOOP` with a rationale, sometimes the `invalid_at` too). It can explain itself, extract the validity date from the evidence, and handle any relation the prompt names.
2. **No model at all**: keep the `exact` judge, let the consolidator and the `wrong` signal find contradictions later.

## Why a System One choice wins here

1. **The write path needs a probability, not prose.** Supersession is an irreversible-looking act to the reader (the old memory disappears from current-state recall), so what the seam must answer is "how sure?" — and a calibrated probability is exactly what a fixed gate needs. A generated verdict has no honest confidence to gate on; parsing one out of the rationale is a second, worse classifier.
2. **Asymmetric harm wants a high, explicit gate.** A false contradiction deprecates a good memory silently; a missed one only delays supersession, because the held-contradiction queue, the `wrong` signal and the consolidator remain ([doc 03 §3](../03-lifecycle.md)). A constant threshold with the reason next to it is the [doc 06 §3](../06-audit-privacy-security.md) poisoning stance applied to the model: the model proposes, the gate disposes.
3. **Evidence is auditable by construction.** Three probabilities, a confidence, a model id and a token count fit in `PROPOSE.details.judge` and read the same for every verdict; a paragraph of rationale does not aggregate, and the store-derived metrics ([ADR-0008](0008-metrics-from-the-store.md)) prefer numbers.
4. **The seam does not change.** One method, same three strings; the dev judges, the tests and the consolidator's dedupe are untouched, and the failure mode is the one [doc 07 §5](../07-operations.md) already demands: fail closed, say so, keep serving.

## Costs accepted

There is no reasoning text — a reviewer of a held contradiction sees three numbers, not why. The `invalid_at` the doc 03 §3 recipe would like extracted from the new evidence stays the episode's `occurred_at`, because a System One model returns no values and this design never fabricates one. The confidence gate is a constant, so a deployment that wants a bolder judge edits code, not configuration — deliberate, until a user asks. And the write path now makes one network call per candidate with neighbors (about half a second, ~500 tokens), bounded by a 10 s timeout that, on expiry, degrades to the model-less behavior rather than blocking the write.
