# ADR-0003 — Staged tier with reinforcement auto-promotion as the default write mode

**Status:** accepted · **Context docs:** [03 §§1–4](../03-lifecycle.md), [06 §3](../06-audit-privacy-security.md)

## Decision

Agent-inferred memories (`llm_inferred`, `agent_observed`) enter a **staged** tier by default: immediately usable but rank-penalized, visibly fenced in context blocks, excluded from high-stakes reads by trust floors, and unable to supersede active memories. They promote to `active` through reinforcement (independent re-observation, useful retrievals, unchallenged tenure, or explicit confirmation). `explicit_user_ask` memories skip staging. Per-agent policy can tighten to `ask`-everything or loosen to silent-active.

## Alternatives considered

- **Ask-first for everything**: maximal safety and auditability, but confirmation fatigue is a product-killer in chat surfaces, and users train themselves to click yes — the safety becomes theater while agents learn slowly.
- **Silent auto-active** (ChatGPT-dreaming-style): zero friction, but it is exactly the non-auditable, poisoning-friendly behavior this design rejects; a single injected message could mint a trusted memory.

## Why staged/auto-promote wins here

1. **It converts time and evidence into trust** — the same mechanism (reinforcement) serves lifecycle promotion, decay reset, and poisoning defense, so the system stays simple.
2. **Quarantine with utility**: staged memories still help (they surface, fenced) so the agent improves from day one, but nothing an agent inferred can steer high-stakes behavior until corroborated.
3. **Human attention is spent where it matters**: `ask` is reserved for sensitive categories, procedural writes, and shared-scope promotions — decisions humans actually should make — instead of being burned on every trivial preference.

## Costs accepted

A window where wrong staged facts can mislead low-stakes answers (mitigated by fencing and the prompt contract's "treat staged as hypotheses"); reinforcement thresholds to tune per category; a review UI becomes a real product requirement (staged triage, doc 07 §6 P2).
