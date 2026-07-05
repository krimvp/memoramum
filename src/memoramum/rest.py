"""REST facade (doc 04 §5): platform code, review UIs, admin, audit.

Identity in P1 is a dev-mode shim: the caller asserts its principal pair
via `X-Memoramum-Actor` / `X-Memoramum-On-Behalf-Of` headers (the
`/v1/context-block` body may carry the pair instead, matching the doc 04
§2.1 example). Real authentication is a deployment concern that replaces
this dependency, not the handlers.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .config import Settings, settings_from_env
from .db import make_pool
from .principals import Flow, Principal, PrincipalError
from .service import AccessDenied, MemoryService, NotFound


def principal_from_headers(
    x_memoramum_actor: str = Header(...),
    x_memoramum_on_behalf_of: str | None = Header(None),
) -> Principal:
    try:
        return Principal(x_memoramum_actor, x_memoramum_on_behalf_of)
    except PrincipalError as e:
        raise HTTPException(status_code=400, detail=str(e))


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


def create_app(service: MemoryService | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or settings_from_env()
    svc = service or MemoryService(make_pool(settings.database_url), settings)
    app = FastAPI(title="memoramum", version="0.1.0")

    @app.exception_handler(AccessDenied)
    async def _denied(request: Request, exc: AccessDenied):
        raise HTTPException(status_code=403, detail=str(exc))

    @app.exception_handler(NotFound)
    async def _missing(request: Request, exc: NotFound):
        raise HTTPException(status_code=404, detail=str(exc))

    @app.exception_handler(ValueError)
    async def _bad(request: Request, exc: ValueError):
        raise HTTPException(status_code=400, detail=str(exc))

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "phase": "P3"}

    @app.post("/v1/context-block")
    def context_block(
        body: ContextBlockRequest,
        x_memoramum_actor: str | None = Header(None),
        x_memoramum_on_behalf_of: str | None = Header(None),
    ):
        if body.principal is not None:
            principal = Principal(body.principal.agent, body.principal.on_behalf_of)
        elif x_memoramum_actor:
            principal = Principal(x_memoramum_actor, x_memoramum_on_behalf_of)
        else:
            raise HTTPException(status_code=400, detail="no principal provided")
        return svc.context_block(
            principal, body.flow.to_flow(), focus=body.focus, token_budget=body.token_budget
        )

    @app.post("/v1/episodes")
    def register_episode(body: EpisodeRequest, principal: Principal = Depends(principal_from_headers)):
        return svc.register_episode(
            principal, scope_id=body.scope_id, source_kind=body.source_kind,
            external_ref=body.external_ref, content=body.content, author=body.author,
            occurred_at=body.occurred_at,
        )

    @app.get("/v1/memories/{memory_id}")
    def get_memory(memory_id: str, principal: Principal = Depends(principal_from_headers)):
        return svc.get_memory(principal, memory_id)

    @app.get("/v1/memories/{memory_id}/history")
    def memory_history(memory_id: str, principal: Principal = Depends(principal_from_headers)):
        return svc.memory_history(principal, memory_id)

    @app.get("/v1/subjects/{subject}/memories")
    def subject_memories(subject: str, principal: Principal = Depends(principal_from_headers)):
        return svc.subject_memories(principal, subject)

    @app.get("/v1/scopes/{scope_id:path}/memories")
    def scope_memories(scope_id: str, principal: Principal = Depends(principal_from_headers)):
        return svc.scope_memories(principal, scope_id)

    @app.get("/v1/audit/events")
    def audit_events(
        action: str | None = None, actor: str | None = None,
        memory_id: str | None = None, scope_id: str | None = None, limit: int = 100,
        principal: Principal = Depends(principal_from_headers),
    ):
        return svc.audit_events(
            principal, action=action, actor=actor, memory_id=memory_id,
            scope_id=scope_id, limit=limit,
        )

    @app.get("/v1/episodes/{episode_id}")
    def get_episode(episode_id: str, principal: Principal = Depends(principal_from_headers)):
        return svc.get_episode(principal, episode_id)

    # --- scope tree & enrollment admin ---

    @app.post("/v1/scopes")
    def create_scope(body: ScopeRequest, principal: Principal = Depends(principal_from_headers)):
        return svc.create_scope(
            principal, scope_id=body.id, family=body.family,
            parent_scope_id=body.parent_scope_id, surface=body.surface,
            external_ref=body.external_ref, trust_class=body.trust_class,
        )

    @app.post("/v1/scopes/{scope_id:path}/relations")
    def set_relation(scope_id: str, body: RelationRequest,
                     principal: Principal = Depends(principal_from_headers)):
        svc.set_relation(principal, scope_id, body.relation, body.principal, remove=body.remove)
        return {"ok": True}

    # --- review surface: staged triage & held contradictions (doc 07 §6 P2) ---

    @app.get("/v1/review/staged")
    def staged_queue(scope_id: str | None = None,
                     principal: Principal = Depends(principal_from_headers)):
        return svc.staged_queue(principal, scope_id=scope_id)

    @app.post("/v1/memories/{memory_id}/review")
    def review_memory(memory_id: str, body: ReviewRequest,
                      principal: Principal = Depends(principal_from_headers)):
        return svc.review_staged(principal, memory_id, action=body.action, note=body.note)

    @app.get("/v1/review/contradictions")
    def contradiction_queue(include_resolved: bool = False,
                            principal: Principal = Depends(principal_from_headers)):
        return svc.contradictions(principal, include_resolved=include_resolved)

    # --- ask confirmations (doc 04 §1, doc 06 §1.2) ---

    @app.get("/v1/review/pending")
    def pending_queue(scope_id: str | None = None,
                      principal: Principal = Depends(principal_from_headers)):
        return svc.pending_queue(principal, scope_id=scope_id)

    @app.post("/v1/review/pending/{pending_id}")
    def resolve_pending(pending_id: str, body: PendingResolutionRequest,
                        principal: Principal = Depends(principal_from_headers)):
        return svc.confirm_pending(principal, pending_id, approved=body.approved,
                                   note=body.note)

    # --- policy administration (doc 05 §5) ---

    @app.get("/v1/policies")
    def list_policies(principal: Principal = Depends(principal_from_headers)):
        return svc.policies(principal)

    @app.post("/v1/policies")
    def put_policy(body: PolicyRequest, principal: Principal = Depends(principal_from_headers)):
        return svc.put_policy(principal, body.document)

    @app.post("/v1/policies/simulate")
    def simulate_policy(body: SimulationRequest,
                        principal: Principal = Depends(principal_from_headers)):
        return svc.simulate_policy(principal, body.document, days=body.days)

    @app.post("/v1/review/contradictions/{queue_id}")
    def resolve_contradiction(queue_id: str, body: ResolutionRequest,
                              principal: Principal = Depends(principal_from_headers)):
        return svc.resolve_contradiction(
            principal, queue_id, resolution=body.resolution,
            invalid_at=body.invalid_at, note=body.note,
        )

    # --- later-phase surface, kept visible and honest ---

    @app.post("/v1/erasure-requests")
    def erasure_requests():
        raise HTTPException(status_code=501, detail="erasure pipeline arrives in P4 (doc 07 §6)")

    @app.post("/v1/quarantine")
    def quarantine():
        raise HTTPException(status_code=501, detail="quarantine tooling arrives in P4 (doc 07 §6)")

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="127.0.0.1", port=8385)
