"""Environment-driven settings for the service."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get(
            "MEMORAMUM_DATABASE_URL",
            "postgresql://memoramum:memoramum@127.0.0.1:5432/memoramum",
        )
    )
    org_scope_id: str = field(
        default_factory=lambda: os.environ.get("MEMORAMUM_ORG_SCOPE", "org:acme")
    )
    # Embedding provider: 'none' (lexical-only retrieval) or 'hash'
    # (deterministic local embedder — dev/test stand-in for a real model).
    embedder: str = field(default_factory=lambda: os.environ.get("MEMORAMUM_EMBEDDER", "none"))

    # Retrieval scoring knobs, doc 04 §3 defaults. Policy-tunable per agent
    # in P3; global here until the policy engine grows that surface.
    status_weights: dict = field(
        default_factory=lambda: {"invariant": 1.2, "active": 1.0, "staged": 0.6}
    )
    # Base decay time constant in days per unit of strength (doc 03 §5 gives
    # the curve R = exp(-t/S) and the kind ratios; the base constant is an
    # implementation default).
    decay_base_days: float = 30.0
    # episodic decays ~4× faster than semantic; procedural ~4× slower.
    decay_kind_factor: dict = field(
        default_factory=lambda: {"semantic": 1.0, "episodic": 0.25, "procedural": 4.0, "profile": 1.0}
    )
    # Scope-proximity weight: position i in the chain scores proximity_base**i.
    scope_proximity_base: float = 0.9
    # Retrieval trust floor when no policy supplies one (doc 05 §4.2).
    default_trust_floor: float = 0.3
    # Hash-chain the event log for these scope families (doc 02 §5).
    hash_chain_families: frozenset = frozenset({"subject"})
    # Context-block budget is in tokens; we approximate tokens as chars/4.
    chars_per_token: int = 4


def settings_from_env() -> Settings:
    return Settings()
