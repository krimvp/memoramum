"""MCP facade (ADR-0005): the primary agent-facing surface.

P4 completes the doc 04 §1 tool surface: the six verbs —
memory_remember, memory_recall, memory_reinforce, memory_forget,
memory_promote, memory_status — plus memory_confirm (the closing half of
every `ask`), and the prompt contract of doc 04 §4 as an MCP prompt. P5
adds the seventh verb, memory_observe: the dev-time episode-registration
path for personal agents whose surface has no platform-side subscriber
(ADR-0012).

The facade runs over two transports. Over **stdio** (this module's entry
point), one server process serves one session context: the principal pair
and the flow (surface, container, participants) come from the environment
the surface integration launches the server with. Over **streamable HTTP**
(mcp_http.py, ADR-0015), the same tools are served remotely: the bearer
token names the actor and the flow arrives as X-Memoramum-* headers.
Either way agents never name raw scope ids — they describe nothing; the
launcher (or the request) already did (doc 04: "the service decides what
that makes visible"). Tools therefore take a `resolve` callable yielding
the (principal, flow) of the current call: process-fixed for stdio,
per-request for HTTP.

    MEMORAMUM_AGENT=agent:sage MEMORAMUM_ON_BEHALF_OF=user:dana \
    MEMORAMUM_SURFACE=slack MEMORAMUM_CONTAINER=channel/C0DEP \
    MEMORAMUM_PARTICIPANTS=user:dana,user:li \
    memoramum-mcp
"""

from __future__ import annotations

import functools
import os
from datetime import datetime
from typing import Callable

import psycopg
from mcp.server.fastmcp import FastMCP
from psycopg_pool import PoolTimeout

from .config import settings_from_env
from .db import make_pool
from .principals import Flow, Principal
from .service import MemoryService, PolicyUnavailable

Identity = Callable[[], tuple[Principal, Flow]]

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


def build_server(service: MemoryService, resolve: Identity) -> FastMCP:
    mcp = FastMCP("memoramum")

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

    return mcp


def main() -> None:
    settings = settings_from_env()
    principal = Principal(
        os.environ["MEMORAMUM_AGENT"], os.environ.get("MEMORAMUM_ON_BEHALF_OF") or None
    )
    participants = tuple(
        p.strip() for p in os.environ.get("MEMORAMUM_PARTICIPANTS", "").split(",") if p.strip()
    )
    touched_paths = tuple(
        p.strip() for p in os.environ.get("MEMORAMUM_TOUCHED_PATHS", "").split(",") if p.strip()
    )
    flow = Flow(
        surface=os.environ.get("MEMORAMUM_SURFACE"),
        container=os.environ.get("MEMORAMUM_CONTAINER"),
        participants=participants,
        session_id=os.environ.get("MEMORAMUM_SESSION"),
        project=os.environ.get("MEMORAMUM_PROJECT") or None,
        touched_paths=touched_paths,
    )
    service = MemoryService(make_pool(settings.database_url), settings)
    build_server(service, lambda: (principal, flow)).run()
