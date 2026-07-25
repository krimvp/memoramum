"""REST facade (doc 04 §5): platform code, review UIs, admin, audit.

Identity: bearer tokens bound to principals (ADR-0014), configured via
MEMORAMUM_API_TOKENS ('principal=token', comma-separated). The token
names the actor; `on_behalf_of` — and the `/v1/context-block` body
principal, which must match the token — stays caller-asserted. The same
door also accepts the OAuth access tokens the MCP endpoint's
authorization server issues (ADR-0016): one issuance story for both
facades, and a user-authorized token proves `on_behalf_of` rather than
asserting it. With neither configured the facade falls back to the
dev-mode shim (the caller asserts its pair via `X-Memoramum-Actor` /
`X-Memoramum-On-Behalf-Of` headers), and `main()` refuses to bind beyond
loopback.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings, make_verifier, resolve_credential, settings_from_env
from .db import make_pool
from .oauth import AccessTokenVerifier, OAuthError, VerifiedCredential, www_authenticate
from .principals import Flow, Principal, PrincipalError
from .service import AccessDenied, MemoryService, NotFound, PolicyUnavailable


class FlowBody(BaseModel):
    surface: str | None = None
    container: str | None = None
    participants: list[str] = Field(default_factory=list)
    session_id: str | None = None

    def to_flow(self) -> Flow:
        return Flow(self.surface, self.container, tuple(self.participants), self.session_id)


class PrincipalBody(BaseModel):
    agent: str
    on_behalf_of: str | None = None


class ContextBlockRequest(BaseModel):
    principal: PrincipalBody | None = None
    flow: FlowBody
    focus: str | None = None
    token_budget: int = 1200


class EpisodeRequest(BaseModel):
    scope_id: str
    source_kind: str
    external_ref: dict[str, Any]
    content: str | None = None
    author: str | None = None
    occurred_at: datetime | None = None


class MembershipSyncRequest(BaseModel):
    surface: str                        # heartbeat after each sync cycle (doc 07 §5)


class ScopeRequest(BaseModel):
    id: str
    family: str
    parent_scope_id: str | None = None
    surface: str | None = None
    external_ref: dict[str, Any] | None = None
    trust_class: str = "internal_public"


class RelationRequest(BaseModel):
    relation: str
    principal: str
    remove: bool = False


class ModulePathsRequest(BaseModel):
    module_scope_id: str
    globs: list[str] = Field(default_factory=list)   # replace semantics (ADR-0010)


class ReviewRequest(BaseModel):
    action: str                    # confirm | reject
    note: str = ""


class ResolutionRequest(BaseModel):
    resolution: str                # supersede | keep_both | reject
    invalid_at: datetime | None = None
    note: str = ""


class PendingResolutionRequest(BaseModel):
    approved: bool
    note: str = ""


class PolicyRequest(BaseModel):
    document: dict[str, Any] | str      # a policy document, or its YAML text (doc 05 §5)


class SimulationRequest(BaseModel):
    document: dict[str, Any] | str
    days: int = 30


class ForgetRequest(BaseModel):
    reason: str = ""
    mode: str = "archive"               # archive | tombstone (doc 03 §6)


class ErasureRequest(BaseModel):
    subject: str                        # 'user:dana'
    legal_basis: str = "gdpr_art_17"
    note: str = ""


class QuarantineRequest(BaseModel):
    """The source predicate of doc 06 §3: episode source, author, agent,
    time window — any combination, at least one."""

    episode_id: str | None = None
    author: str | None = None
    source_kind: str | None = None
    agent: str | None = None
    scope_id: str | None = None
    occurred_from: datetime | None = None
    occurred_to: datetime | None = None
    note: str = ""


class QuarantineResolution(BaseModel):
    memory_id: str
    action: str                         # restore | tombstone
    note: str = ""


class FreezeRequest(BaseModel):
    frozen: bool
    reason: str = ""


def create_app(service: MemoryService | None = None, settings: Settings | None = None,
               verifier: AccessTokenVerifier | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    svc = service or MemoryService(make_pool(settings.database_url), settings)
    app = FastAPI(title="memoramum", version="0.1.0")

    api_tokens = settings.api_tokens
    oauth = settings.oauth
    verifier = verifier or make_verifier(settings)

    def credential(authorization: str | None = Header(None)) -> VerifiedCredential | None:
        """What the presented credential proved — a static principal-bound
        token (ADR-0014) or an OAuth access token (ADR-0016) — or None in
        dev mode."""
        try:
            return resolve_credential(api_tokens, verifier, authorization)
        except OAuthError as e:
            raise HTTPException(
                status_code=e.status, detail=str(e),
                headers={"WWW-Authenticate": www_authenticate(oauth, e.error, str(e))},
            )
        except LookupError as e:
            raise HTTPException(status_code=401, detail=str(e),
                                headers={"WWW-Authenticate": www_authenticate(oauth)})

    def proven_user(cred: VerifiedCredential | None, asserted: str | None) -> str | None:
        """OAuth proves the delegation a static token can only assert: a
        header that disagrees with the user who authorized the token is a
        403, not a quiet override."""
        if cred and cred.on_behalf_of and asserted and asserted != cred.on_behalf_of:
            raise HTTPException(
                status_code=403,
                detail=f"X-Memoramum-On-Behalf-Of {asserted!r} does not match the user "
                       "the access token was authorized by",
            )
        return (cred.on_behalf_of if cred else None) or asserted

    def request_principal(
        cred: VerifiedCredential | None = Depends(credential),
        x_memoramum_actor: str | None = Header(None),
        x_memoramum_on_behalf_of: str | None = Header(None),
    ) -> Principal:
        token_actor = cred.actor if cred else None
        if token_actor and x_memoramum_actor and x_memoramum_actor != token_actor:
            raise HTTPException(
                status_code=403,
                detail=f"X-Memoramum-Actor {x_memoramum_actor!r} does not match the token's principal",
            )
        actor = token_actor or x_memoramum_actor
        if actor is None:
            raise HTTPException(status_code=401,
                                detail="no principal: dev mode requires X-Memoramum-Actor")
        try:
            return Principal(actor, proven_user(cred, x_memoramum_on_behalf_of))
        except PrincipalError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.exception_handler(AccessDenied)
    async def _denied(request: Request, exc: AccessDenied):
        raise HTTPException(status_code=403, detail=str(exc))

    @app.exception_handler(NotFound)
    async def _missing(request: Request, exc: NotFound):
        raise HTTPException(status_code=404, detail=str(exc))

    @app.exception_handler(ValueError)
    async def _bad(request: Request, exc: ValueError):
        raise HTTPException(status_code=400, detail=str(exc))

    # Doc 07 §5: fail closed, and say so. A dead policy engine refuses
    # writes (queue and retry); a dead store degrades surfaces to
    # memoryless operation — the context block is optional enrichment.
    @app.exception_handler(PolicyUnavailable)
    async def _policy_down(request: Request, exc: PolicyUnavailable):
        raise HTTPException(status_code=503, detail=str(exc),
                            headers={"Retry-After": "30"})

    @app.exception_handler(psycopg.OperationalError)
    async def _store_down(request: Request, exc: psycopg.OperationalError):
        raise HTTPException(
            status_code=503, headers={"Retry-After": "30"},
            detail="memory service unavailable — degrade to memoryless operation (doc 07 §5)",
        )

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "phase": "P5"}

    @app.post("/v1/context-block")
    def context_block(
        body: ContextBlockRequest,
        cred: VerifiedCredential | None = Depends(credential),
        x_memoramum_actor: str | None = Header(None),
        x_memoramum_on_behalf_of: str | None = Header(None),
    ):
        token_actor = cred.actor if cred else None
        if body.principal is not None:
            actor, on_behalf_of = body.principal.agent, body.principal.on_behalf_of
        elif x_memoramum_actor:
            actor, on_behalf_of = x_memoramum_actor, x_memoramum_on_behalf_of
        else:
            actor, on_behalf_of = token_actor, x_memoramum_on_behalf_of
        if actor is None:
            raise HTTPException(status_code=400, detail="no principal provided")
        if token_actor and actor != token_actor:
            raise HTTPException(
                status_code=403,
                detail=f"asserted principal {actor!r} does not match the token's principal",
            )
        principal = Principal(actor, proven_user(cred, on_behalf_of))
        return svc.context_block(
            principal, body.flow.to_flow(), focus=body.focus, token_budget=body.token_budget
        )

    @app.post("/v1/episodes")
    def register_episode(body: EpisodeRequest, principal: Principal = Depends(request_principal)):
        return svc.register_episode(
            principal, scope_id=body.scope_id, source_kind=body.source_kind,
            external_ref=body.external_ref, content=body.content, author=body.author,
            occurred_at=body.occurred_at,
        )

    @app.post("/v1/membership-sync")
    def membership_sync(body: MembershipSyncRequest,
                        principal: Principal = Depends(request_principal)):
        return svc.record_membership_sync(principal, surface=body.surface)

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str, principal: Principal = Depends(request_principal)):
        return svc.get_memory(principal, memory_id)

    @app.get("/v1/memories/{memory_id}/history")
    def memory_history(memory_id: str, principal: Principal = Depends(request_principal)):
        return svc.memory_history(principal, memory_id)

    @app.get("/v1/subjects/{subject}/memories")
    def subject_memories(subject: str, principal: Principal = Depends(request_principal)):
        return svc.subject_memories(principal, subject)

    @app.get("/v1/scopes/{scope_id:path}/memories")
    def scope_memories(scope_id: str, principal: Principal = Depends(request_principal)):
        return svc.scope_memories(principal, scope_id)

    @app.get("/v1/audit/events")
    def audit_events(
        action: str | None = None, actor: str | None = None,
        memory_id: str | None = None, scope_id: str | None = None, limit: int = 100,
        principal: Principal = Depends(request_principal),
    ):
        return svc.audit_events(
            principal, action=action, actor=actor, memory_id=memory_id,
            scope_id=scope_id, limit=limit,
        )

    @app.get("/v1/metrics")
    def metrics(days: int = 30, principal: Principal = Depends(request_principal)):
        return svc.metrics(principal, days=days)

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(episode_id: str, principal: Principal = Depends(request_principal)):
        return svc.get_episode(principal, episode_id)

    # --- scope tree & enrollment admin ---

    @app.post("/v1/scopes")
    def create_scope(body: ScopeRequest, principal: Principal = Depends(request_principal)):
        return svc.create_scope(
            principal, scope_id=body.id, family=body.family,
            parent_scope_id=body.parent_scope_id, surface=body.surface,
            external_ref=body.external_ref, trust_class=body.trust_class,
        )

    @app.post("/v1/scopes/{scope_id:path}/relations")
    def set_relation(scope_id: str, body: RelationRequest,
                     principal: Principal = Depends(request_principal)):
        svc.set_relation(principal, scope_id, body.relation, body.principal, remove=body.remove)
        return {"ok": True}

    # The ADR-0010 module-boundary mapping: repository path globs → module
    # scope (org-admin/system; replace semantics).
    @app.post("/v1/module-paths")
    def set_module_paths(body: ModulePathsRequest,
                         principal: Principal = Depends(request_principal)):
        return svc.set_module_paths(
            principal, module_scope_id=body.module_scope_id, globs=body.globs
        )

    # --- review surface: staged triage & held contradictions (doc 07 §6 P2) ---

    @app.get("/v1/review/staged")
    def staged_queue(scope_id: str | None = None,
                     principal: Principal = Depends(request_principal)):
        return svc.staged_queue(principal, scope_id=scope_id)

    @app.post("/v1/memories/{memory_id}/review")
    def review_memory(memory_id: str, body: ReviewRequest,
                      principal: Principal = Depends(request_principal)):
        return svc.review_staged(principal, memory_id, action=body.action, note=body.note)

    @app.get("/v1/review/contradictions")
    def contradiction_queue(include_resolved: bool = False,
                            principal: Principal = Depends(request_principal)):
        return svc.contradictions(principal, include_resolved=include_resolved)

    # --- ask confirmations (doc 04 §1, doc 06 §1.2) ---

    @app.get("/v1/review/pending")
    def pending_queue(scope_id: str | None = None,
                      principal: Principal = Depends(request_principal)):
        return svc.pending_queue(principal, scope_id=scope_id)

    @app.post("/v1/review/pending/{pending_id}")
    def resolve_pending(pending_id: str, body: PendingResolutionRequest,
                        principal: Principal = Depends(request_principal)):
        return svc.confirm_pending(principal, pending_id, approved=body.approved,
                                   note=body.note)

    # --- policy administration (doc 05 §5) ---

    @app.get("/v1/policies")
    def list_policies(principal: Principal = Depends(request_principal)):
        return svc.policies(principal)

    @app.post("/v1/policies")
    def put_policy(body: PolicyRequest, principal: Principal = Depends(request_principal)):
        return svc.put_policy(principal, body.document)

    @app.post("/v1/policies/simulate")
    def simulate_policy(body: SimulationRequest,
                        principal: Principal = Depends(request_principal)):
        return svc.simulate_policy(principal, body.document, days=body.days)

    @app.post("/v1/review/contradictions/{queue_id}")
    def resolve_contradiction(queue_id: str, body: ResolutionRequest,
                              principal: Principal = Depends(request_principal)):
        return svc.resolve_contradiction(
            principal, queue_id, resolution=body.resolution,
            invalid_at=body.invalid_at, note=body.note,
        )

    # --- forgetting & incident response (doc 03 §6, doc 06 §2–3) ---

    @app.post("/v1/memories/{memory_id}/forget")
    def forget_memory(memory_id: str, body: ForgetRequest,
                      principal: Principal = Depends(request_principal)):
        return svc.forget(principal, Flow(), memory_id=memory_id,
                          reason=body.reason, mode=body.mode)

    @app.post("/v1/erasure-requests")
    def request_erasure(body: ErasureRequest,
                        principal: Principal = Depends(request_principal)):
        return svc.request_erasure(principal, subject=body.subject,
                                   legal_basis=body.legal_basis, note=body.note)

    @app.get("/v1/erasure-requests/{request_id}")
    def erasure_request(request_id: str,
                        principal: Principal = Depends(request_principal)):
        return svc.erasure_request(principal, request_id)

    @app.post("/v1/quarantine")
    def quarantine(body: QuarantineRequest,
                   principal: Principal = Depends(request_principal)):
        return svc.quarantine(
            principal, episode_id=body.episode_id, author=body.author,
            source_kind=body.source_kind, agent=body.agent, scope_id=body.scope_id,
            occurred_from=body.occurred_from, occurred_to=body.occurred_to, note=body.note,
        )

    @app.get("/v1/quarantine")
    def quarantine_queue(include_resolved: bool = False,
                         principal: Principal = Depends(request_principal)):
        return svc.quarantine_queue(principal, include_resolved=include_resolved)

    @app.post("/v1/quarantine/{request_id}")
    def resolve_quarantine(request_id: str, body: QuarantineResolution,
                           principal: Principal = Depends(request_principal)):
        return svc.resolve_quarantine(principal, request_id, body.memory_id,
                                      action=body.action, note=body.note)

    @app.post("/v1/agents/{agent}/freeze")
    def freeze_agent(agent: str, body: FreezeRequest,
                     principal: Principal = Depends(request_principal)):
        return svc.set_agent_freeze(principal, agent, frozen=body.frozen,
                                    reason=body.reason)

    return app


def main() -> None:
    import uvicorn

    settings = settings_from_env()
    host = os.environ.get("MEMORAMUM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("MEMORAMUM_API_PORT", "8385"))
    authenticated = settings.api_tokens or settings.oauth.enabled
    if not authenticated and host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit(
            "refusing to bind beyond loopback without MEMORAMUM_API_TOKENS or "
            "MEMORAMUM_OAUTH_ISSUER/_RESOURCE — the dev-mode shim trusts asserted "
            "principals (ADR-0014, ADR-0016)"
        )
    uvicorn.run(create_app(settings=settings), host=host, port=port)
