"""OAuth 2.1 resource-server verification for the remote facades (ADR-0016).

The MCP authorization spec makes a remote MCP endpoint an OAuth 2.0
**resource server**: it publishes protected-resource metadata (RFC 9728)
naming the deployment's authorization server, answers an unauthenticated
call with `401` + a `WWW-Authenticate` header pointing at that metadata,
and validates the access token the client returns with. Memoramum is
never the authorization server — login, consent, and client registration
stay with the org's IdP (ADR-0016). This module is the verification half
plus the claim → principal mapping that keeps the audit invariant intact
(ADR-0006, ADR-0014): every `actor` an event records was proven, never
asserted.

Configuration (all `MEMORAMUM_OAUTH_*`; the facades run OAuth only when
`ISSUER` and `RESOURCE` are both set):

    MEMORAMUM_OAUTH_ISSUER=https://idp.acme.example
    MEMORAMUM_OAUTH_RESOURCE=https://memory.acme.example/mcp
    MEMORAMUM_OAUTH_SCOPES=memoramum.mcp
    MEMORAMUM_OAUTH_CLIENT_AGENTS=cli-9f3=agent:sage
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

import anyio.to_thread
import httpx
import jwt

from .principals import PRINCIPAL_KINDS, validate_principal

# Tolerated clock skew when checking `exp` / `nbf` / `iat`.
CLOCK_SKEW_SECONDS = 60

# RFC 9728 §3.1: where a protected resource publishes its metadata.
WELL_KNOWN_RESOURCE = "/.well-known/oauth-protected-resource"


class OAuthError(LookupError):
    """A presented access token was refused. LookupError so both facades'
    existing credential-refusal paths (ADR-0014) handle it unchanged."""

    def __init__(self, detail: str, error: str = "invalid_token", status: int = 401):
        super().__init__(detail)
        self.error = error
        self.status = status


def parse_client_agents(spec: str) -> tuple[tuple[str, str], ...]:
    """Parse MEMORAMUM_OAUTH_CLIENT_AGENTS: comma-separated
    `oauth_client_id=principal` entries, e.g. 'cli-9f3=agent:sage'. A
    non-empty mapping is an allowlist — a token from an unlisted client is
    refused rather than falling back to its subject."""
    pairs: list[tuple[str, str]] = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        client_id, sep, principal = entry.partition("=")
        if not sep or not client_id.strip() or not principal.strip():
            raise ValueError(
                f"MEMORAMUM_OAUTH_CLIENT_AGENTS entry {entry!r}: expected 'client_id=principal'"
            )
        pairs.append((client_id.strip(), validate_principal(principal.strip())))
    return tuple(pairs)


def _split(spec: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in spec.replace(",", " ").split() if p.strip())


@dataclass(frozen=True)
class OAuthSettings:
    """Resource-server configuration. `enabled` is the whole switch: with
    no issuer/resource the facades behave exactly as before (ADR-0014
    static tokens, or the loopback-only dev shim)."""

    # Authorization server that issues tokens for this resource server.
    issuer: str = ""
    # This deployment's canonical MCP URL: the RFC 8707 resource
    # indicator clients must request, the `aud` tokens must carry, and the
    # base for the RFC 9728 metadata path.
    resource: str = ""
    # Overrides for what discovery would otherwise supply.
    jwks_url: str = ""
    audience: str = ""
    algorithms: tuple[str, ...] = ("RS256", "ES256")
    # Scopes every token must carry (also advertised as scopes_supported).
    required_scopes: tuple[str, ...] = ()
    # Claim mapping. An IdP that knows Memoramum principals can name the
    # actor outright; otherwise the client → agent allowlist decides, and
    # failing that the subject acts for itself.
    actor_claim: str = "memoramum_actor"
    subject_claim: str = "sub"
    subject_prefix: str = "user:"
    client_agents: tuple[tuple[str, str], ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.resource)

    @property
    def token_audience(self) -> str:
        return self.audience or self.resource

    @property
    def metadata_path(self) -> str:
        """RFC 9728 §3.1: the resource's path is inserted after the
        well-known segment, so `https://host/mcp` publishes at
        `/.well-known/oauth-protected-resource/mcp`."""
        path = urlparse(self.resource).path.rstrip("/")
        return f"{WELL_KNOWN_RESOURCE}{path}"

    @property
    def metadata_url(self) -> str:
        parsed = urlparse(self.resource)
        return f"{parsed.scheme}://{parsed.netloc}{self.metadata_path}"

    def metadata(self) -> dict:
        """The RFC 9728 protected-resource document."""
        doc: dict[str, Any] = {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "memoramum",
        }
        if self.required_scopes:
            doc["scopes_supported"] = list(self.required_scopes)
        return doc


def oauth_settings_from_env(environ: dict[str, str] | None = None) -> OAuthSettings:
    import os

    env = environ if environ is not None else os.environ
    algorithms = _split(env.get("MEMORAMUM_OAUTH_ALGORITHMS", ""))
    return OAuthSettings(
        issuer=env.get("MEMORAMUM_OAUTH_ISSUER", "").strip().rstrip("/"),
        resource=env.get("MEMORAMUM_OAUTH_RESOURCE", "").strip(),
        jwks_url=env.get("MEMORAMUM_OAUTH_JWKS_URL", "").strip(),
        audience=env.get("MEMORAMUM_OAUTH_AUDIENCE", "").strip(),
        algorithms=algorithms or ("RS256", "ES256"),
        required_scopes=_split(env.get("MEMORAMUM_OAUTH_SCOPES", "")),
        actor_claim=env.get("MEMORAMUM_OAUTH_ACTOR_CLAIM", "memoramum_actor").strip(),
        subject_claim=env.get("MEMORAMUM_OAUTH_SUBJECT_CLAIM", "sub").strip(),
        subject_prefix=env.get("MEMORAMUM_OAUTH_SUBJECT_PREFIX", "user:").strip(),
        client_agents=parse_client_agents(env.get("MEMORAMUM_OAUTH_CLIENT_AGENTS", "")),
    )


@dataclass(frozen=True)
class VerifiedCredential:
    """What a presented credential proved. `on_behalf_of` is set only by
    OAuth: a user-authorized token proves the delegation that a static
    token can only assert (ADR-0014 costs)."""

    actor: str
    on_behalf_of: str | None = None
    scopes: tuple[str, ...] = ()
    source: str = "oauth"          # 'oauth' | 'static'
    claims: dict = field(default_factory=dict, repr=False)


def principal_from_claim(value: str, prefix: str) -> str:
    """A claim value is either already a principal ('user:dana') or a bare
    identifier the deployment prefixes ('dana' → 'user:dana')."""
    kind, sep, rest = value.partition(":")
    if sep and kind in PRINCIPAL_KINDS and rest:
        return validate_principal(value)
    return validate_principal(f"{prefix}{value}")


def looks_like_jwt(credential: str) -> bool:
    """Static tokens (ADR-0014) are opaque deployment config; access
    tokens from the IdP are JWS-compact. Shape decides what an
    unrecognized credential is worth verifying as, so both credential
    kinds can share one door."""
    parts = credential.split(".")
    return len(parts) == 3 and all(parts[:2])


class AccessTokenVerifier:
    """Validates a JWT access token against the authorization server's
    JWKS and maps its claims onto a principal pair.

    The signing keys are fetched over HTTPS and cached by PyJWT's JWKS
    client; `verify` blocks, `averify` offloads to a worker thread so the
    ASGI door never stalls the event loop on a key refresh."""

    def __init__(self, settings: OAuthSettings, jwk_client: Any | None = None,
                 http_get: Callable[[str], dict] | None = None):
        if not settings.enabled:
            raise ValueError("OAuth is not configured: set issuer and resource")
        self.settings = settings
        self._jwk_client = jwk_client
        self._http_get = http_get or _get_json
        self._lock = threading.Lock()

    # -- key discovery ---------------------------------------------------

    def _discover_jwks_url(self) -> str:
        if self.settings.jwks_url:
            return self.settings.jwks_url
        issuer = self.settings.issuer
        parsed = urlparse(issuer)
        base, path = f"{parsed.scheme}://{parsed.netloc}", parsed.path.rstrip("/")
        candidates = [
            # RFC 8414 path insertion, then the OIDC-style suffix form.
            f"{base}/.well-known/oauth-authorization-server{path}",
            f"{base}/.well-known/openid-configuration{path}",
            f"{issuer}/.well-known/openid-configuration",
        ]
        errors: list[str] = []
        for url in candidates:
            try:
                document = self._http_get(url)
            except Exception as e:                       # network / non-200 / non-JSON
                errors.append(f"{url}: {e}")
                continue
            if document.get("issuer", issuer).rstrip("/") != issuer:
                errors.append(f"{url}: issuer {document.get('issuer')!r} is not {issuer!r}")
                continue
            jwks_uri = document.get("jwks_uri")
            if jwks_uri:
                return jwks_uri
            errors.append(f"{url}: no jwks_uri")
        raise OAuthError(
            "cannot discover the authorization server's signing keys "
            f"({'; '.join(errors)}) — set MEMORAMUM_OAUTH_JWKS_URL",
            error="temporarily_unavailable", status=503,
        )

    def _keys(self):
        with self._lock:
            if self._jwk_client is None:
                self._jwk_client = jwt.PyJWKClient(
                    self._discover_jwks_url(), cache_keys=True, lifespan=300
                )
            return self._jwk_client

    # -- verification ----------------------------------------------------

    def verify(self, token: str) -> VerifiedCredential:
        try:
            signing_key = self._keys().get_signing_key_from_jwt(token)
        except OAuthError:
            raise
        except Exception as e:
            raise OAuthError(f"cannot verify token signature: {e}")
        try:
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.settings.algorithms),
                audience=self.settings.token_audience,
                issuer=self.settings.issuer,
                leeway=CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError as e:
            raise OAuthError(f"invalid access token: {e}")
        return self._credential(claims)

    async def averify(self, token: str) -> VerifiedCredential:
        return await anyio.to_thread.run_sync(self.verify, token)

    # -- claims → principal pair ----------------------------------------

    def _credential(self, claims: dict) -> VerifiedCredential:
        cfg = self.settings
        scopes = _token_scopes(claims)
        missing = [s for s in cfg.required_scopes if s not in scopes]
        if missing:
            raise OAuthError(
                f"access token is missing required scope(s): {', '.join(missing)}",
                error="insufficient_scope", status=403,
            )

        client_id = str(claims.get("azp") or claims.get("client_id") or "")
        subject_raw = claims.get(cfg.subject_claim)
        subject: str | None = None
        # A client-credentials token names the client as its own subject —
        # that is a machine, not a human to act on behalf of.
        if subject_raw and str(subject_raw) != client_id:
            try:
                subject = principal_from_claim(str(subject_raw), cfg.subject_prefix)
            except ValueError as e:
                raise OAuthError(f"claim {cfg.subject_claim!r}: {e}")

        actor_raw = claims.get(cfg.actor_claim)
        if actor_raw:
            try:
                actor = validate_principal(str(actor_raw))
            except ValueError as e:
                raise OAuthError(f"claim {cfg.actor_claim!r}: {e}")
        elif cfg.client_agents:
            mapped = dict(cfg.client_agents).get(client_id)
            if mapped is None:
                raise OAuthError(
                    f"OAuth client {client_id!r} is not enrolled as a Memoramum principal"
                )
            actor = mapped
        elif subject is not None:
            actor = subject                       # a human acting directly
        else:
            raise OAuthError(
                f"access token carries no principal: no {cfg.actor_claim!r} claim, "
                "no enrolled client, no subject"
            )

        on_behalf_of = subject if subject and subject != actor else None
        if on_behalf_of and not on_behalf_of.startswith("user:"):
            raise OAuthError(f"token subject {on_behalf_of!r} is not a user principal")
        return VerifiedCredential(actor, on_behalf_of, scopes, "oauth", claims)


def _token_scopes(claims: dict) -> tuple[str, ...]:
    scope = claims.get("scope")
    if isinstance(scope, str):
        return tuple(scope.split())
    scp = claims.get("scp")
    if isinstance(scp, str):
        return tuple(scp.split())
    if isinstance(scp, (list, tuple)):
        return tuple(str(s) for s in scp)
    return ()


def _get_json(url: str) -> dict:
    response = httpx.get(url, timeout=5.0, follow_redirects=True)
    response.raise_for_status()
    return response.json()


def www_authenticate(oauth: OAuthSettings | None, error: str | None = None,
                     description: str | None = None) -> str:
    """The `WWW-Authenticate` challenge. With OAuth configured it carries
    the `resource_metadata` pointer an MCP client follows to find the
    authorization server and start the login it never had to be told
    about (RFC 9728 §5.1). Without it the bare ADR-0014 challenge stands —
    a static-token deployment has nothing to discover."""
    if not (oauth and oauth.enabled):
        return "Bearer"
    fields = [f'resource_metadata="{oauth.metadata_url}"']
    if error:
        fields.append(f'error="{error}"')
    if description:
        fields.append(f'error_description="{_quotable(description)}"')
    return "Bearer " + ", ".join(fields)


def _quotable(value: str) -> str:
    """RFC 7235 quoted-string: no bare quotes or backslashes, one line."""
    return value.replace("\\", " ").replace('"', "'").replace("\n", " ").strip()
