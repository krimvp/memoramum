"""Streamable-HTTP MCP facade (ADR-0015): the remote agent-facing surface.

The same seven-verb tool surface as the stdio facade (mcp_server.py),
served at `/mcp` on the REST deployment for clients that are not
co-located with the service — the personal dev-time harnesses of
ADR-0012. Authentication is ADR-0014's principal-bound bearer tokens:
the token names the actor of every tool call; the flow context a stdio
launcher would assert via environment variables arrives instead as
per-request headers, the same caller-asserted trust:

    Authorization:               Bearer <token bound to agent:...>
    X-Memoramum-On-Behalf-Of:    user:dana          (asserted, as everywhere)
    X-Memoramum-Surface:         ide
    X-Memoramum-Container:       devsession/DS1
    X-Memoramum-Participants:    user:dana,user:li  (comma-separated)
    X-Memoramum-Session:         sess-1
    X-Memoramum-Project:         project/mono       (dev-time flows, ADR-0012)
    X-Memoramum-Touched-Paths:   src/auth/login.py  (comma-separated, ADR-0010)

With no tokens configured the facade runs the same header-asserted
dev-mode shim as REST (X-Memoramum-Actor names the actor) — which the
CLI entry point already refuses to expose beyond loopback (ADR-0014).

The transport is stateless: each request carries its own identity, so one
process serves many principals — isolation rests on the token → principal
binding, which is why tokens are issued per agent principal (ADR-0015).
"""

from __future__ import annotations

import json
from typing import Any

from .config import Settings, resolve_bearer_actor
from .mcp_server import build_server
from .principals import Flow, Principal, PrincipalError
from .service import MemoryService

_IDENTITY = "memoramum.identity"


def _flow_from_headers(headers: dict[str, str]) -> Flow:
    def csv(name: str) -> tuple[str, ...]:
        return tuple(p.strip() for p in (headers.get(name) or "").split(",") if p.strip())

    return Flow(
        surface=headers.get("x-memoramum-surface"),
        container=headers.get("x-memoramum-container"),
        participants=csv("x-memoramum-participants"),
        session_id=headers.get("x-memoramum-session"),
        project=headers.get("x-memoramum-project") or None,
        touched_paths=csv("x-memoramum-touched-paths"),
    )


async def _reject(send: Any, status: int, detail: str, *, bearer: bool = False) -> None:
    headers = [(b"content-type", b"application/json")]
    if bearer:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body",
                "body": json.dumps({"detail": detail}).encode()})


class MCPHttpEndpoint:
    """ASGI endpoint for `Route("/mcp", ...)`: authenticate, resolve the
    (principal, flow) of the request into its scope, then hand off to the
    stateless streamable-HTTP session manager. Tools read the identity
    back through the SDK's request context — the transport threads the
    HTTP request into every tool invocation."""

    def __init__(self, service: MemoryService, settings: Settings):
        self._settings = settings
        self._mcp = build_server(service, self._identity)
        self._mcp.settings.stateless_http = True
        self._mcp.settings.json_response = True
        self._mcp.streamable_http_app()  # instantiates the session manager

    @property
    def session_manager(self):
        """Run `session_manager.run()` in the host app's lifespan."""
        return self._mcp.session_manager

    def _identity(self) -> tuple[Principal, Flow]:
        request = self._mcp.get_context().request_context.request
        return request.scope[_IDENTITY]

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        try:
            token_actor = resolve_bearer_actor(
                self._settings.api_tokens, headers.get("authorization")
            )
        except LookupError as e:
            return await _reject(send, 401, str(e), bearer=True)
        asserted = headers.get("x-memoramum-actor")
        if token_actor and asserted and asserted != token_actor:
            return await _reject(
                send, 403,
                f"X-Memoramum-Actor {asserted!r} does not match the token's principal",
            )
        actor = token_actor or asserted
        if actor is None:
            return await _reject(send, 401,
                                 "no principal: dev mode requires X-Memoramum-Actor")
        try:
            principal = Principal(actor, headers.get("x-memoramum-on-behalf-of") or None)
        except PrincipalError as e:
            return await _reject(send, 400, str(e))
        scope = dict(scope)
        scope[_IDENTITY] = (principal, _flow_from_headers(headers))
        await self._mcp.session_manager.handle_request(scope, receive, send)
