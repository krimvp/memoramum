"""The P5 exit criterion (doc 07 §6): the module-scope story, end-to-end
across the personal dev-time surface (surface:ide) and GitLab — a
dev-time observation landing in a shared module scope; an MR review
producing candidates across four touched modules; one staying MR-scoped
and one promoted mr→module through the module_owner ask; two sibling
modules holding contradicting conventions that the consolidator never
compares; and a later MR that touches one module surfacing only that
module's convention.

Runs against a service wired with the deterministic dev stand-ins for the
LLM-shaped seams (marker extractor, overlap judge, hash embedder), in a
scenario-private slice of the scope tree so the shared fixtures — and the
sibling test_module_scopes slice — stay untouched."""

from datetime import datetime, timedelta, timezone

from conftest import ADMIN, SYSTEM, TEST_DB

import pytest

from memoramum.config import Settings
from memoramum.consolidator import Consolidator
from memoramum.extraction import ExtractionWorker
from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied, MemoryService

PROJECT = "project/scn-mono"
AUTH = "module:scn-mono/auth"
PAYMENTS = "module:scn-mono/payments"
BILLING = "module:scn-mono/billing"
SEARCH = "module:scn-mono/search"
MODULES = (AUTH, PAYMENTS, BILLING, SEARCH)
MR, MR2 = "mr/scn-900", "mr/scn-901"
DEVSESSION = "devsession/scn-DS1"

DEV = Principal("agent:scn-dev")                       # a bare dev-time agent
DEV_FOR_REV = Principal("agent:scn-dev", "user:scn-rev")  # acting for a member
REV = Principal("user:scn-rev")                        # a project member, not owner
PAY_OWNER = Principal("user:scn-pay-owner")            # the payments module_owner


@pytest.fixture(scope="module")
def scn(pool, svc):
    s = svc.create_scope
    s(SYSTEM, scope_id=PROJECT, family="container", parent_scope_id="surface:gitlab",
      surface="gitlab")
    for module in MODULES:
        s(SYSTEM, scope_id=module, family="module", parent_scope_id=PROJECT)
    for container in (MR, MR2):
        s(SYSTEM, scope_id=container, family="container", parent_scope_id=PROJECT,
          surface="gitlab")
    # A personal dev-time session on surface:ide. surface:ide is a shared
    # singleton owned by the platform (its scope id is claimed elsewhere); this
    # private slice parents its session under the org and carries surface='ide'
    # so the BUILTIN_IDE_SURFACE_LAYER (ADR-0012/0013) still applies — the raw
    # chain reaches the org either way, then readability filters it.
    s(SYSTEM, scope_id=DEVSESSION, family="container", parent_scope_id="org:acme",
      surface="ide", trust_class="private")

    r = svc.set_relation
    for user in ("user:scn-dev", "user:scn-rev"):
        r(SYSTEM, PROJECT, "member", user)
    # agent:scn-dev reads/writes the project (so modules + MRs resolve) and its
    # own dev session; it is never enrolled org-wide.
    for scope in (PROJECT, DEVSESSION):
        r(SYSTEM, scope, "reader_agent", "agent:scn-dev")
        r(SYSTEM, scope, "writer_agent", "agent:scn-dev")
    # The ADR-0011 confirmer tuple — authoritative, independent of membership.
    r(SYSTEM, PAYMENTS, "module_owner", "user:scn-pay-owner")

    # ADR-0010 boundaries: one glob per module.
    svc.set_module_paths(SYSTEM, module_scope_id=AUTH, globs=["src/auth/*"])
    svc.set_module_paths(SYSTEM, module_scope_id=PAYMENTS, globs=["src/payments/*"])
    svc.set_module_paths(SYSTEM, module_scope_id=BILLING, globs=["src/billing/*"])
    svc.set_module_paths(SYSTEM, module_scope_id=SEARCH, globs=["src/search/*"])
    return MemoryService(pool, Settings(database_url=TEST_DB, embedder="hash",
                                        judge="overlap", extractor="marker"))


def _ide_flow(paths):
    return Flow(surface="ide", container=DEVSESSION, project=PROJECT,
               touched_paths=tuple(paths))


def _mr_comment(scn, content, *, path, author, when):
    return scn.register_episode(
        SYSTEM, scope_id=MR, source_kind="mr_comment",
        external_ref={"paths": [path], "mr": "!900"},
        content=content, author=author, occurred_at=when,
    )


def test_the_module_scope_scenario(scn):
    now = datetime.now(timezone.utc)

    # -- Step 1: Dev-time observation → a shared module scope. A personal
    # agent on surface:ide self-reports an episode (the memory_observe path,
    # ADR-0012: no platform subscriber for a laptop), then writes a codebase
    # convention. The builtin ide routing (ADR-0013) lands it staged in the
    # touched module scope — visible to the module's team, not the agent's
    # private notebook — at the lower dev_observation trust base.
    ep = scn.register_episode(
        DEV, scope_id=DEVSESSION, source_kind="dev_observation",
        external_ref={"paths": ["src/auth/login.py"]},
        content="noticed auth tokens time out on the login flow", author="user:scn-dev",
    )
    conv = scn.remember(
        DEV, _ide_flow(["src/auth/login.py"]),
        content="Auth tokens expire after fifteen minutes of inactivity",
        kind="semantic", origin_kind="agent_observed", categories=["codebase_convention"],
        source_episode_ids=[ep["id"]],
    )
    assert conv["decision"] == "stage"                 # staged by default (ADR-0013)
    auth_conv = conv["memory_id"]
    mem = scn.status(SYSTEM, memory_id=auth_conv)["memory"]
    assert mem["scope_id"] == AUTH                      # a shared scope, not agent:scn-dev
    assert mem["trust_score"] == pytest.approx(0.45)    # self-reported dev_observation

    # A personal task tactic, by contrast, stays in the agent's own scope —
    # only the agent itself may read it (doc 07 §5), so check the row directly.
    tac = scn.remember(
        DEV, _ide_flow(["src/auth/login.py"]),
        content="I grep for call sites before renaming a shared symbol",
        kind="procedural", origin_kind="agent_observed", categories=["task_tactic"],
    )
    with scn.pool.connection() as conn:
        tac_scope = conn.cursor().execute(
            "SELECT scope_id FROM memories WHERE id=%s", (tac["memory_id"],)
        ).fetchone()["scope_id"]
    assert tac_scope == "agent:scn-dev"

    # -- Step 2: MR review → four candidates across four touched modules.
    # Marker extraction over the MR comments proposes one candidate per
    # comment; each stays narrow in the MR source scope, carrying the module
    # hint the batch's paths touched (issue #15) for later promotion tooling.
    comments = [
        ("Session cookies are marked HttpOnly and Secure", "src/auth/session.py"),
        ("Payment amounts are stored as integer minor units", "src/payments/amount.py"),
        ("Invoices are generated as immutable PDF documents", "src/billing/invoice.py"),
        ("Query strings are lowercased before indexing", "src/search/index.py"),
    ]
    for text, path in comments:
        _mr_comment(scn, f"convention: {text}", path=path, author="user:scn-rev",
                    when=now - timedelta(hours=1))
    summary = ExtractionWorker(scn).run(now, only_scopes=[MR])
    proposed = summary[MR]["proposed"]
    assert len(proposed) == 4
    assert all(p["decision"] == "stage" for p in proposed)
    by_content = {p["content"]: p["memory_id"] for p in proposed}
    assert set(by_content) == {text for text, _ in comments}
    assert all(scn.get_memory(SYSTEM, mid)["scope_id"] == MR for mid in by_content.values())
    queue = scn.staged_queue(ADMIN, scope_id=MR)
    hinted = {q["content"]: q["modules_touched"] for q in queue if q["content"] in by_content}
    assert len(hinted) == 4
    for hint in hinted.values():
        assert set(hint) == set(MODULES)                # provenance carries the hints

    # -- Step 3: One stays MR-scoped, one promoted. The payments candidate
    # crosses mr → module: the gate asks, and only the module_owner may
    # confirm (ADR-0011) — a project member who is not the owner cannot.
    pay_cand = by_content["Payment amounts are stored as integer minor units"]
    mr_specific = by_content["Query strings are lowercased before indexing"]
    out = scn.promote(DEV, Flow(surface="gitlab", container=MR),
                      memory_id=pay_cand, target_scope=PAYMENTS,
                      justification="the payments team should own this")
    assert out["decision"] == "ask"
    with pytest.raises(AccessDenied):
        scn.confirm_pending(REV, out["pending_id"], approved=True)
    scn.confirm_pending(PAY_OWNER, out["pending_id"], approved=True)
    assert scn.get_memory(SYSTEM, pay_cand)["scope_id"] == PAYMENTS
    actions = [e["action"] for e in scn.memory_history(ADMIN, pay_cand)]
    assert "PROMOTE_SCOPE" in actions and "CONFIRM" in actions
    # …and a genuinely MR-specific candidate stays put at MR scope.
    assert scn.get_memory(SYSTEM, mr_specific)["scope_id"] == MR

    # -- Step 4: Sibling-module contradiction isolation. Two sibling modules
    # take contradicting naming conventions. Each write's chain reaches only
    # its own module (the paths touched), so neither write sees the other —
    # no contradiction is raised, and the consolidator's full job set never
    # compares across sibling scopes (ADR-0009; every job is per scope).
    camel = scn.remember(
        DEV, _ide_flow(["src/payments/ids.py"]),
        content="Identifiers in the payments module use camelCase naming",
        kind="semantic", origin_kind="agent_observed", categories=["codebase_convention"],
    )
    snake = scn.remember(
        DEV, _ide_flow(["src/billing/ids.py"]),
        content="Identifiers in the billing module use snake_case naming",
        kind="semantic", origin_kind="agent_observed", categories=["codebase_convention"],
    )
    camel_id, snake_id = camel["memory_id"], snake["memory_id"]
    assert scn.get_memory(SYSTEM, camel_id)["scope_id"] == PAYMENTS
    assert scn.get_memory(SYSTEM, snake_id)["scope_id"] == BILLING
    assert "contradicts" not in camel and "supersedes" not in camel
    assert "contradicts" not in snake and "supersedes" not in snake

    result = Consolidator(scn).run(now)
    # The overlap judge WOULD call these two a contradiction if it ever
    # compared them; the per-scope-chain boundary means it never does.
    for mid in (camel_id, snake_id):
        row = scn.get_memory(SYSTEM, mid)
        assert row["status"] != "deprecated" and row["superseded_by"] is None
    assert not any({camel_id, snake_id} <= set(m["merged"]) for m in result["merged"])
    queued = {(str(q["challenger_id"]), str(q["contradicted_id"]))
              for q in scn.contradictions(ADMIN, include_resolved=True)}
    assert (camel_id, snake_id) not in queued and (snake_id, camel_id) not in queued

    # -- Step 5: A later MR touches one module. Its chain surfaces that
    # module's promoted convention and none of the other modules' — the
    # module topology narrows visibility exactly as intended.
    mr2_flow = Flow(surface="gitlab", container=MR2, participants=("user:scn-rev",),
                    touched_paths=("src/payments/refund.py",))
    ids = [h["id"] for h in scn.recall(DEV_FOR_REV, mr2_flow,
                                       query="how are payment amounts stored")]
    assert pay_cand in ids                              # its own module's convention
    assert auth_conv not in ids and snake_id not in ids  # other modules stay out

    present = set(scn.context_block(DEV_FOR_REV, mr2_flow, token_budget=4000)["memory_ids"])
    assert {pay_cand, camel_id} <= present              # both payments conventions
    for absent in (auth_conv, snake_id, mr_specific):
        assert absent not in present                    # auth / billing / other-MR do not
