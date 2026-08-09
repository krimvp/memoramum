"""Environment-driven settings for the service."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

from .principals import validate_principal


def parse_api_tokens(spec: str) -> tuple[tuple[str, str], ...]:
    """Parse MEMORAMUM_API_TOKENS: comma-separated `principal=token`
    entries (ADR-0014), e.g. 'user:admin=S3CRET,system:ingest=OTHER'.
    Tokens therefore cannot contain a comma. Empty spec = no tokens =
    the REST facade's unauthenticated dev mode."""
    pairs: list[tuple[str, str]] = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        principal, sep, token = entry.partition("=")
        if not sep or not token.strip():
            raise ValueError(
                f"MEMORAMUM_API_TOKENS entry {entry!r}: expected 'principal=token'"
            )
        pairs.append((validate_principal(principal.strip()), token.strip()))
    return tuple(pairs)


def resolve_bearer_actor(
    api_tokens: tuple[tuple[str, str], ...], authorization: str | None
) -> str | None:
    """The ADR-0014 resolver, shared by both facades (REST and the
    streamable-HTTP MCP endpoint, ADR-0015): the principal bound to the
    presented bearer credential. Returns None when no tokens are
    configured (the dev-mode shim); raises LookupError for a missing or
    unknown credential."""
    if not api_tokens:
        return None
    credential = ""
    if authorization:
        scheme, _, rest = authorization.partition(" ")
        if scheme.lower() == "bearer":
            credential = rest.strip()
    if not credential:
        raise LookupError("bearer token required")
    actor = None
    for principal, token in api_tokens:      # constant-shape scan, no early exit
        if secrets.compare_digest(credential, token):
            actor = principal
    if actor is None:
        raise LookupError("unknown bearer token")
    return actor


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

    # REST bearer tokens, each bound to one principal (ADR-0014). Empty =
    # the dev-mode header shim; the CLI then refuses non-loopback binds.
    api_tokens: tuple = field(
        default_factory=lambda: parse_api_tokens(os.environ.get("MEMORAMUM_API_TOKENS", ""))
    )

    # Host allowlist for the remote MCP endpoint (ADR-0015). Non-empty:
    # DNS-rebinding protection validates Host against it (entries may end
    # ':*' for any port). Empty with tokens configured: protection off —
    # bearer auth already fences browser-originated calls, and the public
    # hostname is the deployment's business. Empty without tokens: the
    # SDK's loopback-only default guards the dev-mode shim.
    mcp_allowed_hosts: tuple = field(
        default_factory=lambda: tuple(
            h.strip()
            for h in os.environ.get("MEMORAMUM_MCP_ALLOWED_HOSTS", "").split(",")
            if h.strip()
        )
    )

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

    # Retrieval scoring knobs, doc 04 §3 defaults. The policy engine tunes
    # both halves of the read policy per agent: the gates (trust floor,
    # sensitivity ceiling, staged inclusion, category deny-lists — doc 05
    # §4.2) and the score-factor exponents (ADR-0018), which fall back to
    # `score_weights` below when no layer sets them.
    status_weights: dict = field(
        default_factory=lambda: {"invariant": 1.2, "active": 1.0, "staged": 0.6}
    )
    # Exponents on the five doc 04 §3 factors: 0 disables a factor, 1 is
    # the documented default, >1 sharpens it (ADR-0018).
    score_weights: dict = field(
        default_factory=lambda: {
            "relevance": 1.0, "retention": 1.0, "trust": 1.0,
            "status": 1.0, "scope_proximity": 1.0,
        }
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

    # Per-leg admission gates (ADR-0016). These bound what counts as
    # relevant at all, so they are properties of the embedding space and of
    # the deployment — not per-agent weights.
    #
    # Cosine distance beyond which a vector neighbour is not a candidate.
    # Like `outlier_distance` above, the default is calibrated for the dev
    # hash embedder, whose vectors are near-orthogonal noise; a real model
    # embedder clusters far tighter and wants roughly 0.4–0.6.
    vector_distance_ceiling: float = 0.85
    # pg_trgm word similarity a query must reach against its best-matching
    # extent of a memory's content to enter the literal leg (ADR-0017).
    # Postgres' own default for `<%` is 0.6; long prose queries essentially
    # never clear it, which is what keeps the leg identifier-shaped.
    word_similarity_threshold: float = 0.6
    # HNSW search breadth for the vector leg. Retrieval always filters
    # (scope chain, status, trust floor) and HNSW filters after the index
    # walk, so the default ef_search of 40 can under-return on a selective
    # chain (doc 07 §3). Iterative scans are used on top where the pgvector
    # build offers them.
    hnsw_ef_search: int = 200
    # Hash-chain the event log for these scope families (doc 02 §5).
    hash_chain_families: frozenset = frozenset({"subject"})
    # Context-block budget is in tokens; we approximate tokens as chars/4.
    chars_per_token: int = 4


def settings_from_env() -> Settings:
    return Settings()
