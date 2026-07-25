"""Environment-driven settings for the service."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field

from .oauth import (
    AccessTokenVerifier,
    OAuthSettings,
    VerifiedCredential,
    looks_like_jwt,
    oauth_settings_from_env,
)
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


def bearer_credential(authorization: str | None) -> str:
    """The credential out of an `Authorization: Bearer …` header, or ''."""
    if authorization:
        scheme, _, rest = authorization.partition(" ")
        if scheme.lower() == "bearer":
            return rest.strip()
    return ""


def match_static_token(
    api_tokens: tuple[tuple[str, str], ...], credential: str
) -> VerifiedCredential | None:
    """ADR-0014: the principal a deployment-configured token is bound to,
    or None if the credential is not one of them."""
    actor = None
    for principal, token in api_tokens:      # constant-shape scan, no early exit
        if secrets.compare_digest(credential, token):
            actor = principal
    return VerifiedCredential(actor, source="static") if actor else None


def plan_credential(
    api_tokens: tuple[tuple[str, str], ...],
    verifier: AccessTokenVerifier | None,
    authorization: str | None,
) -> tuple[VerifiedCredential | None, str]:
    """The transport-independent half of the door both facades share (REST
    and the streamable-HTTP MCP endpoint, ADR-0015). Returns either what
    the credential already proved, or the access token still to verify.

    Two credential kinds meet here on the seam ADR-0014 isolated: an
    opaque static token from MEMORAMUM_API_TOKENS, matched first so a
    configured credential is never mistaken for anything else, and an
    OAuth access token from the deployment's authorization server
    (ADR-0016), recognized by its JWS shape. `(None, "")` means neither is
    configured — the dev-mode shim. Raises LookupError for a missing or
    unrecognized credential."""
    if not api_tokens and verifier is None:
        return None, ""
    credential = bearer_credential(authorization)
    if not credential:
        raise LookupError("bearer token required")
    matched = match_static_token(api_tokens, credential)
    if matched is not None:
        return matched, ""
    if verifier is not None and looks_like_jwt(credential):
        return None, credential
    raise LookupError("unknown bearer token")


def resolve_credential(
    api_tokens: tuple[tuple[str, str], ...],
    verifier: AccessTokenVerifier | None,
    authorization: str | None,
) -> VerifiedCredential | None:
    """What the presented credential proved (blocking; the async door in
    the MCP facade offloads the verification step to a worker thread)."""
    resolved, token = plan_credential(api_tokens, verifier, authorization)
    if token:
        assert verifier is not None
        return verifier.verify(token)
    return resolved


def resolve_bearer_actor(
    api_tokens: tuple[tuple[str, str], ...], authorization: str | None
) -> str | None:
    """The ADR-0014 resolver in its static-token-only form: the principal
    bound to the presented bearer token, or None in dev mode."""
    resolved = resolve_credential(api_tokens, None, authorization)
    return resolved.actor if resolved else None


def make_verifier(settings: "Settings") -> AccessTokenVerifier | None:
    """The OAuth resource-server verifier, or None when this deployment
    has no authorization server configured (ADR-0016)."""
    return AccessTokenVerifier(settings.oauth) if settings.oauth.enabled else None


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

    # OAuth 2.1 resource-server configuration (ADR-0016): the remote MCP
    # endpoint publishes protected-resource metadata and accepts access
    # tokens the deployment's authorization server issued for it. Unset =
    # OAuth off, static tokens only. Both credential kinds resolve through
    # the same seam, so tokens and OAuth can run side by side.
    oauth: OAuthSettings = field(default_factory=oauth_settings_from_env)

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
