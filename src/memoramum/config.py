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
    # Contradiction judge (doc 03 §3): 'exact' (duplicates only — write-time
    # contradiction detection off without a model) or 'overlap'
    # (deterministic dev/test stand-in). A model judge slots in behind the
    # same interface (lifecycle.Judge).
    judge: str = field(default_factory=lambda: os.environ.get("MEMORAMUM_JUDGE", "exact"))
    # PII analyzer (doc 06 §4): 'regex' (deterministic pattern stand-in for
    # a Presidio-class model — same seam) or 'none' (scanning off).
    pii_analyzer: str = field(default_factory=lambda: os.environ.get("MEMORAMUM_PII", "regex"))
    # Background extraction (doc 07 §1): 'marker' (deterministic dev
    # stand-in — standing statements flagged by their author) or 'none'
    # (extraction off). A model extractor slots in behind the same seam.
    extractor: str = field(default_factory=lambda: os.environ.get("MEMORAMUM_EXTRACTOR", "marker"))
    # Reflection/summarization (doc 07 §2): 'none' (off — reflection is
    # LLM-shaped work, like the judge) or 'theme' (deterministic dev
    # stand-in clustering episodic memories by categories + subjects).
    reflector: str = field(default_factory=lambda: os.environ.get("MEMORAMUM_REFLECTOR", "none"))

    # Extraction debounce (doc 07 §1): accumulate, run at conversation-lull,
    # never wait past the cap.
    extraction_debounce_minutes: int = 30
    extraction_cap_hours: int = 4

    # Reflection: minimum cluster of related episodic memories to distill.
    reflection_min_cluster: int = 3

    # Poisoning anomaly checks (doc 06 §3 detection, doc 07 §2 hygiene).
    anomaly_author_daily_writes: int = 30    # staged writes traced to one author per 24 h
    outlier_min_scope_size: int = 5          # embedding-outlier check needs a population
    # Cosine distance from the scope centroid that counts as an outlier.
    # Real embeddings cluster far tighter than this; the dev hash embedder
    # is near-orthogonal noise, so the default is deliberately high.
    outlier_distance: float = 0.9

    # Membership-sync staleness bound (doc 07 §5): beyond this many seconds
    # since a surface's last sync heartbeat, private-trust-class scopes on
    # that surface fail closed and other reads note the staleness in their
    # READ event. Only surfaces that heartbeat (membership_sync table) are
    # bounded — membership authored directly in the service can't go stale.
    membership_staleness_bound_seconds: int = 300

    # Erasure attestations (doc 06 §2.2 step 5) are HMAC-signed with this
    # key; a real deployment injects one (MEMORAMUM_ATTESTATION_KEY).
    attestation_key: str = field(
        default_factory=lambda: os.environ.get("MEMORAMUM_ATTESTATION_KEY", "dev-attestation-key")
    )

    # Status-promotion rule, doc 03 §4 defaults (global; the doc's
    # "per-category tunable" remains future policy surface).
    promote_reobservations: int = 1
    promote_useful_retrievals: int = 2
    promote_floor_days: int = 3        # agent_observed from non-member authors
    promote_tenure_days: int | None = None  # unchallenged tenure, off by default

    # Storage-side sweeps, doc 03 §5 defaults.
    staged_idle_archive_days: int = 30       # staged, never retrieved, unreinforced
    contradiction_escalate_days: int = 7     # held contradiction → escalate to review
    contradiction_archive_days: int = 30     # held contradiction → archive challenger
    decay_archive_threshold: float = 0.05    # active, R below this → archive candidate
    decay_archive_grace_days: float = 7.0    # candidates archive on a later run (visibility first)
    history_retention_days: int = 365        # deprecated older than this → archive

    # Retrieval scoring knobs, doc 04 §3 defaults. The policy engine (P3)
    # tunes the read-side gates per agent (trust floor, sensitivity
    # ceiling, staged inclusion, category deny-lists — doc 05 §4.2); the
    # score weights themselves stay global defaults.
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
