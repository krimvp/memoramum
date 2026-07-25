"""MCP facade (ADR-0005): the only agent-facing surface.

P4 completes the doc 04 §1 tool surface: the six verbs —
memory_remember, memory_recall, memory_reinforce, memory_forget,
memory_promote, memory_status — plus memory_confirm (the closing half of
every `ask`), and the prompt contract of doc 04 §4 as an MCP prompt. P5
adds the seventh verb, memory_observe: the dev-time episode-registration
path for personal agents whose surface has no platform-side subscriber
(ADR-0012).

One tool registry, two transports (ADR-0015):

**stdio** — one server process, one session context: the principal pair
and the flow (surface, container, participants) come from the environment
the surface integration launches the server with. Agents therefore never
name raw scope ids — they describe nothing; the launcher already did
(doc 04: "the service decides what that makes visible").

    MEMORAMUM_AGENT=agent:sage MEMORAMUM_ON_BEHALF_OF=user:dana \\
    MEMORAMUM_SURFACE=slack MEMORAMUM_CONTAINER=channel/C0DEP \\
    MEMORAMUM_PARTICIPANTS=user:dana,user:li \\
    memoramum-mcp

**streamable HTTP** — the central deployment's endpoint: harnesses
connect remotely instead of spawning anything. The credential at the door
names the actor; the flow rides per-request `X-Memoramum-*` headers set
by the connecting harness — or the connect URL's query string, for a
client whose whole configuration is a URL (ADR-0017) — the same trust
stdio extends to the launcher's environment. Stateless: any replica
serves any call.

    https://memory.acme.example/mcp?surface=ide&project=project/platform-api

Two credential kinds share the door:

*OAuth 2.1* (ADR-0016) — the endpoint is a resource server: it publishes
protected-resource metadata (RFC 9728), challenges an anonymous call with
`401` + a `WWW-Authenticate` pointing at it, and the MCP client logs the
human in at the deployment's authorization server without being told how.
A user-authorized token proves `on_behalf_of` instead of asserting it.

    MEMORAMUM_MCP_TRANSPORT=http MEMORAMUM_MCP_HOST=0.0.0.0 \\
    MEMORAMUM_OAUTH_ISSUER=https://idp.acme.example \\
    MEMORAMUM_OAUTH_RESOURCE=https://memory.acme.example/mcp \\
    MEMORAMUM_OAUTH_CLIENT_AGENTS=cli-9f3=agent:sage \\
    memoramum-mcp

*Static tokens* (ADR-0014) — the same principal-bound
MEMORAMUM_API_TOKENS as the REST facade, for platform components and
deployments with no IdP.

    MEMORAMUM_MCP_TRANSPORT=http MEMORAMUM_MCP_HOST=0.0.0.0 \\
    MEMORAMUM_API_TOKENS=agent:sage=S3CRET \\
    memoramum-mcp
"""

from __future__ import annotations

import functools
import json
import os
from datetime import datetime
from typing import Callable

import psycopg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from psycopg_pool import PoolTimeout
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Settings, make_verifier, plan_credential, settings_from_env
from .db import make_pool
from .oauth import (
    WELL_KNOWN_RESOURCE,
    AccessTokenVerifier,
    OAuthError,
    OAuthSettings,
    VerifiedCredential,
    www_authenticate,
)
from .principals import Flow, Principal, PrincipalError
from .service import MemoryService, PolicyUnavailable

PROMPT_CONTRACT = """\
You have a memory service (Memoramum). Use it under this contract:

Remember when: the user explicitly asks ("remember / note / don't forget")
— use origin_kind=explicit_user_ask and verbatim-faithful content; the
user corrects you or states a durable preference or constraint; you learn
a stable fact about a team, process, or system that future sessions will
need; you complete something the hard way and the lesson generalizes
(kind=episodic, and say so in justification).

Do not remember: secrets, credentials, tokens (policy-denied — do not
attempt); transient state unless with explicit valid_at/short-TTL intent;
third-party personal information beyond what the flow requires; speculation
phrased as fact — mark uncertainty in the content or don't write.

Recall when: before answering anything about a person, team, process, or
past decision — check first, don't guess; when the user references shared
history ("like last time", "the usual"); before redoing discovery you may
have done before.

Conduct: honor the verdicts — a `deny` is final for this write, do not
rephrase to evade; relay `ask` questions verbatim; never claim to have
remembered or forgotten something unless the tool confirmed it; prefer
citing provenance for memory-derived claims ("per @dana in #deploys in
March"); treat [staged] items as hypotheses — verify before acting on them
in consequential ways; if a tool returns `unavailable`, say "I can't check
my memory right now" — don't guess, and don't claim memory you couldn't
reach.
"""

# (Principal, Flow) per call: constant in stdio mode, request-derived in
# HTTP mode.
Resolver = Callable[[], tuple[Principal, Flow]]


def _degrades(fn):
    """Doc 07 §5: a dead store or policy engine yields an explicit
    unavailability answer — the agent degrades to memoryless operation
    and says so, it never guesses."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (psycopg.OperationalError, PoolTimeout, PolicyUnavailable) as e:
            return {"unavailable": True, "reason": str(e),
                    "say": "I can't check my memory right now."}

    return wrapper


def _register(mcp: FastMCP, service: MemoryService, resolve: Resolver) -> None:
    """The doc 04 §1 tool surface, transport-independent."""

    @mcp.tool()
    @_degrades
    def memory_remember(
        content: str,
        kind: str = "semantic",
        origin_kind: str = "explicit_user_ask",
        subjects: list[str] | None = None,
        categories: list[str] | None = None,
        justification: str = "",
        source_episode_ids: list[str] | None = None,
        valid_at: datetime | None = None,
    ) -> dict:
        """Store one atomic memory. origin_kind MUST reflect reality:
        explicit_user_ask only when the user actually asked. The response is
        the policy verdict (allow | stage | ask | deny) — honor it; a deny is
        final for this write."""
        principal, flow = resolve()
        return service.remember(
            principal, flow, content=content, kind=kind, origin_kind=origin_kind,
            subjects=subjects, categories=categories, justification=justification,
            source_episode_ids=source_episode_ids, valid_at=valid_at,
        )

    @mcp.tool()
    @_degrades
    def memory_recall(
        query: str,
        kinds: list[str] | None = None,
        subjects: list[str] | None = None,
        include_staged: bool = True,
        limit: int = 8,
    ) -> list[dict] | dict:
        """Search memory mid-task (deliberate recall). Use before answering
        anything about a person, team, process, or past decision. Each result
        carries a provenance hint — weigh it, and cite it where useful."""
        principal, flow = resolve()
        return service.recall(
            principal, flow, query=query, kinds=kinds, subjects=subjects,
            include_staged=include_staged, limit=limit,
        )

    @mcp.tool()
    @_degrades
    def memory_reinforce(memory_id: str, signal: str = "useful", note: str = "") -> dict:
        """Feedback on a recalled memory. signal='useful' when you used it
        and it was right (this is what earns staged memories their active
        status); signal='wrong' when it misled — that lowers confidence and
        queues it for contradiction review, no forget powers needed."""
        principal, flow = resolve()
        return service.reinforce(principal, flow, memory_id=memory_id, signal=signal, note=note)

    @mcp.tool()
    @_degrades
    def memory_forget(memory_id: str, reason: str = "", mode: str = "archive") -> dict:
        """Forget a memory (mode 'archive', or 'tombstone' for hard
        erasure). Policy-gated: on your own you may only forget memories
        in your own agent scope or ones you authored that are still
        staged — anything else returns `ask`; relay the ask_prompt and
        close it with memory_confirm. Use with the user's ask ("forget
        that") to fulfil a user forget — never claim to have forgotten
        unless the tool confirmed it."""
        principal, flow = resolve()
        return service.forget(principal, flow, memory_id=memory_id, reason=reason, mode=mode)

    @mcp.tool()
    @_degrades
    def memory_promote(memory_id: str, target_scope: str, justification: str = "") -> dict:
        """Move a memory to a broader scope (e.g. channel → workspace) or a
        subject scope. Almost always returns `ask` — relay the ask_prompt to
        the human verbatim and close it with memory_confirm. The one tool
        where you name a scope; the service validates you may write there."""
        principal, flow = resolve()
        return service.promote(
            principal, flow, memory_id=memory_id, target_scope=target_scope,
            justification=justification,
        )

    @mcp.tool()
    @_degrades
    def memory_confirm(pending_id: str, approved: bool, note: str = "") -> dict:
        """Close an `ask`: relay the user's answer to a pending question from
        memory_remember or memory_promote. Only report what the user actually
        said — the confirmation is recorded as the human's decision."""
        principal, _ = resolve()
        return service.confirm_pending(principal, pending_id, approved=approved, note=note)

    @mcp.tool()
    @_degrades
    def memory_observe(
        content: str,
        paths: list[str] | None = None,
        ref: str | None = None,
        occurred_at: datetime | None = None,
    ) -> dict:
        """Register a dev-time observation as an episode in this session's
        scope (ADR-0012). No platform-side subscriber exists for a local
        session, so the client self-reports; these episodes carry a lower
        trust base than platform-verified sources. `paths` are the files the
        observation is about — they route codebase conventions to the right
        module scope. Extraction (not this tool) proposes memories from what
        you observe; enrollment still gates the write."""
        principal, flow = resolve()
        external_ref: dict = {"paths": list(paths or [])}
        if ref:
            external_ref["ref"] = ref
        return service.register_episode(
            principal, scope_id=flow.container, source_kind="dev_observation",
            external_ref=external_ref, content=content,
            author=principal.effective_user, occurred_at=occurred_at,
        )

    @mcp.tool()
    @_degrades
    def memory_status(
        memory_id: str | None = None,
        subject: str | None = None,
        scope: str | None = None,
    ) -> dict:
        """Introspect memory: pass exactly one of memory_id (lifecycle state,
        provenance, event digest), subject (everything known about a
        principal, e.g. 'user:dana'), or scope (a scope's inventory). This is
        how "what do you know about me?" gets answered in-surface."""
        principal, _ = resolve()
        return service.status(principal, memory_id=memory_id, subject=subject, scope=scope)

    @mcp.prompt()
    def prompt_contract() -> str:
        """The Memoramum prompt contract: when to remember and recall."""
        return PROMPT_CONTRACT


def build_server(service: MemoryService, principal: Principal, flow: Flow) -> FastMCP:
    """The stdio shape: one server, one session context (ADR-0005). The
    prompt contract (doc 04 §4) ships as the server's `instructions` as
    well as a prompt, so a client that reads neither still receives it on
    initialize — the contract only works if the agent sees it."""
    mcp = FastMCP("memoramum", instructions=PROMPT_CONTRACT)
    _register(mcp, service, lambda: (principal, flow))
    return mcp


def _split(header: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in header.split(",") if p.strip())


def build_remote_server(service: MemoryService, settings: Settings) -> FastMCP:
    """The streamable-HTTP shape (ADR-0015): one central endpoint, the
    principal pair and flow resolved per request. What the credential
    proved is stashed on the ASGI scope by _BearerAuthMiddleware: always
    the actor, and — for a user-authorized OAuth token (ADR-0016) —
    `on_behalf_of` too. Anything the credential did not prove stays a
    caller-asserted header, the same trust stdio extends to the launcher's
    environment variables."""
    if settings.mcp_allowed_hosts:
        security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(settings.mcp_allowed_hosts),
        )
    elif settings.api_tokens or settings.oauth.enabled:
        # Rebinding needs a browser to relay calls, and a browser cannot
        # attach the bearer token; Host stays the deployment edge's concern.
        security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    else:
        security = None    # dev-mode shim: the SDK's loopback-only default
    mcp = FastMCP("memoramum", instructions=PROMPT_CONTRACT, stateless_http=True,
                  transport_security=security)

    def resolve() -> tuple[Principal, Flow]:
        request = mcp.get_context().request_context.request
        if request is None:
            raise PrincipalError("no HTTP request context to resolve a principal from")
        headers = request.headers
        query = request.query_params

        def stated(field: str, header: str) -> str:
            """A flow field as this call states it: the per-request header
            where the harness can set one, else the connect URL's query
            string — the only channel every MCP client has (ADR-0017).
            Same assertion, two carriers; the header is the live one, so
            it wins."""
            return headers.get(header) or query.get(field, "")

        credential: VerifiedCredential | None = request.scope.get("memoramum.credential")
        token_actor = credential.actor if credential else None
        asserted = stated("actor", "x-memoramum-actor") or None
        if token_actor and asserted and asserted != token_actor:
            raise PrincipalError(
                f"X-Memoramum-Actor {asserted!r} does not match the token's principal"
            )
        actor = token_actor or asserted
        if actor is None:
            raise PrincipalError(
                "no principal: dev mode requires X-Memoramum-Actor (or ?actor= on the URL)"
            )
        asserted_user = stated("on_behalf_of", "x-memoramum-on-behalf-of") or None
        proven_user = credential.on_behalf_of if credential else None
        if proven_user and asserted_user and asserted_user != proven_user:
            raise PrincipalError(
                f"X-Memoramum-On-Behalf-Of {asserted_user!r} does not match the "
                f"user the access token was authorized by"
            )
        principal = Principal(actor, proven_user or asserted_user)
        flow = Flow(
            surface=stated("surface", "x-memoramum-surface") or None,
            container=stated("container", "x-memoramum-container") or None,
            participants=_split(stated("participants", "x-memoramum-participants")),
            session_id=stated("session", "x-memoramum-session") or None,
            project=stated("project", "x-memoramum-project") or None,
            touched_paths=_split(stated("paths", "x-memoramum-touched-paths")),
        )
        return principal, flow

    _register(mcp, service, resolve)
    return mcp


def _metadata_routes(oauth: OAuthSettings) -> list[Route]:
    """RFC 9728 discovery: the document a challenged MCP client fetches to
    learn which authorization server to log in at (ADR-0016). Public by
    definition — the door below lets it through unauthenticated — and
    served at both the path-inserted URL (§3.1, what the challenge points
    at) and the bare well-known path older clients try first."""
    document = oauth.metadata()

    async def metadata(request: Request) -> JSONResponse:
        return JSONResponse(document, headers={"Access-Control-Allow-Origin": "*"})

    paths = [oauth.metadata_path]
    if oauth.metadata_path != WELL_KNOWN_RESOURCE:
        paths.append(WELL_KNOWN_RESOURCE)
    return [Route(p, endpoint=metadata, methods=["GET", "OPTIONS"]) for p in paths]


class _BearerAuthMiddleware:
    """The MCP door: a missing, malformed, or unknown credential is
    refused with HTTP 401 before any JSON-RPC processing, and what the
    credential proved rides the ASGI scope so per-call resolution never
    re-derives identity from a raw secret. Both credential kinds land
    here — an OAuth access token (ADR-0016), whose challenge names the
    authorization server to log in at, and a static principal-bound token
    (ADR-0014). With neither configured this is a pass-through — the
    dev-mode header shim applies (loopback only)."""

    def __init__(self, app, api_tokens: tuple[tuple[str, str], ...],
                 oauth: OAuthSettings | None = None,
                 verifier: AccessTokenVerifier | None = None):
        self.app = app
        self.api_tokens = api_tokens
        self.oauth = oauth
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (not self.api_tokens and self.verifier is None):
            return await self.app(scope, receive, send)
        if scope.get("path", "").startswith(WELL_KNOWN_RESOURCE):
            return await self.app(scope, receive, send)   # discovery is public
        authorization = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                authorization = value.decode("latin-1")
        try:
            credential = await self._resolve(authorization)
        except OAuthError as e:
            return await self._refuse(send, str(e), error=e.error, status=e.status)
        except LookupError as e:
            return await self._refuse(send, str(e))
        scope = dict(scope)
        scope["memoramum.credential"] = credential
        scope["memoramum.actor"] = credential.actor
        await self.app(scope, receive, send)

    async def _resolve(self, authorization: str) -> VerifiedCredential:
        resolved, token = plan_credential(self.api_tokens, self.verifier, authorization)
        if token:
            return await self.verifier.averify(token)
        assert resolved is not None    # a configured door always resolves or raises
        return resolved

    async def _refuse(self, send, detail: str, error: str = "invalid_token",
                      status: int = 401) -> None:
        body = json.dumps({"detail": detail}).encode()
        challenge = www_authenticate(self.oauth, error, detail).encode()
        await send({
            "type": "http.response.start", "status": status,
            "headers": [(b"content-type", b"application/json"),
                        (b"www-authenticate", challenge),
                        (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


class _CorsMiddleware:
    """Browser-hosted MCP clients can only follow the ADR-0016 login if
    they can read the challenge: `WWW-Authenticate` is invisible to fetch()
    unless exposed, and the preflight has to pass before the 401 is ever
    seen. Applied only when a credential door is configured — in dev mode
    the shim trusts asserted principals, and no web page should be able to
    reach it from the user's browser."""

    HEADERS = (
        (b"access-control-allow-origin", b"*"),
        (b"access-control-expose-headers", b"WWW-Authenticate, Mcp-Session-Id, Mcp-Protocol-Version"),
    )
    PREFLIGHT = HEADERS + (
        (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
        (b"access-control-allow-headers",
         b"Authorization, Content-Type, Mcp-Session-Id, Mcp-Protocol-Version, Last-Event-ID"),
        (b"access-control-max-age", b"86400"),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("method") == "OPTIONS":
            await send({"type": "http.response.start", "status": 204,
                        "headers": [*self.PREFLIGHT, (b"content-length", b"0")]})
            return await send({"type": "http.response.body", "body": b""})

        async def with_cors(message):
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = [*message.get("headers", []), *self.HEADERS]
            await send(message)

        await self.app(scope, receive, with_cors)


def create_mcp_app(service: MemoryService | None = None, settings: Settings | None = None,
                   verifier: AccessTokenVerifier | None = None):
    """The remote facade as an ASGI app (mounted at /mcp), auth included:
    the OAuth discovery routes (ADR-0016) in front, the credential door
    around everything else, CORS outermost so a browser client can see
    the challenge it must answer."""
    settings = settings or settings_from_env()
    svc = service or MemoryService(make_pool(settings.database_url), settings)
    oauth = settings.oauth
    verifier = verifier or make_verifier(settings)
    app = build_remote_server(svc, settings).streamable_http_app()
    if oauth.enabled:
        app.router.routes[:0] = _metadata_routes(oauth)
    door = _BearerAuthMiddleware(app, settings.api_tokens, oauth, verifier)
    return _CorsMiddleware(door) if (settings.api_tokens or verifier) else door


def main() -> None:
    settings = settings_from_env()
    transport = os.environ.get("MEMORAMUM_MCP_TRANSPORT", "stdio")

    if transport == "http":
        import uvicorn

        host = os.environ.get("MEMORAMUM_MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MEMORAMUM_MCP_PORT", "8386"))
        authenticated = settings.api_tokens or settings.oauth.enabled
        if not authenticated and host not in ("127.0.0.1", "::1", "localhost"):
            raise SystemExit(
                "refusing to bind beyond loopback without MEMORAMUM_API_TOKENS or "
                "MEMORAMUM_OAUTH_ISSUER/_RESOURCE — the dev-mode shim trusts asserted "
                "principals (ADR-0014, ADR-0015, ADR-0016)"
            )
        uvicorn.run(create_mcp_app(settings=settings), host=host, port=port)
        return
    if transport != "stdio":
        raise SystemExit(f"unknown MEMORAMUM_MCP_TRANSPORT {transport!r}: expected stdio or http")

    principal = Principal(
        os.environ["MEMORAMUM_AGENT"], os.environ.get("MEMORAMUM_ON_BEHALF_OF") or None
    )
    flow = Flow(
        surface=os.environ.get("MEMORAMUM_SURFACE"),
        container=os.environ.get("MEMORAMUM_CONTAINER"),
        participants=_split(os.environ.get("MEMORAMUM_PARTICIPANTS", "")),
        session_id=os.environ.get("MEMORAMUM_SESSION"),
        project=os.environ.get("MEMORAMUM_PROJECT") or None,
        touched_paths=_split(os.environ.get("MEMORAMUM_TOUCHED_PATHS", "")),
    )
    service = MemoryService(make_pool(settings.database_url), settings)
    build_server(service, principal, flow).run()
