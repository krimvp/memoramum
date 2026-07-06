"""Module scopes (ADR-0009/0010/0011/0012/0013), the P5 surface (doc 07 §6).

Runs in a module-scoped private slice of the scope tree so the shared
worked-scenario fixtures stay untouched: a GitLab project with three
module scopes, their path-glob boundaries, an owner and a maintainer, plus
a personal dev-time surface (surface:ide → a private devsession)."""

from datetime import datetime, timezone

from conftest import SYSTEM, TEST_DB

import pytest
from fastapi.testclient import TestClient

from memoramum import policy, scopes
from memoramum.config import Settings
from memoramum.principals import Flow, Principal
from memoramum.rest import create_app
from memoramum.service import AccessDenied, MemoryService

PROJECT = "project/mod-api"
CORE = "module:mod-api/core"
PAYMENTS = "module:mod-api/payments"
BILLING = "module:mod-api/billing"
MR = "mr/7001"
IDE = "surface:ide"
DEVSESSION = "devsession/DS1"

DEV_AGENT = Principal("agent:dev")                    # a bare dev-time agent
DEV_FOR_DEV = Principal("agent:dev", "user:dev")      # acting for a member
DEV_FOR_OUT = Principal("agent:dev", "user:outsider")  # acting for a non-member
MOD_OWNER = Principal("user:mod-owner")
MOD_MAINT = Principal("user:mod-maint")
ROOT = Principal("user:root")                          # org owner (from the seed)

MR_FLOW = Flow(surface="gitlab", container=MR)
NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def mod(svc):
    s = svc.create_scope
    s(SYSTEM, scope_id=PROJECT, family="container", parent_scope_id="surface:gitlab",
      surface="gitlab")
    for module in (CORE, PAYMENTS, BILLING):
        s(SYSTEM, scope_id=module, family="module", parent_scope_id=PROJECT)
    s(SYSTEM, scope_id=MR, family="container", parent_scope_id=PROJECT, surface="gitlab")
    # The personal dev-time surface: a private devsession under surface:ide.
    s(SYSTEM, scope_id=IDE, family="surface", parent_scope_id="org:acme", surface="ide")
    s(SYSTEM, scope_id=DEVSESSION, family="container", parent_scope_id=IDE,
      surface="ide", trust_class="private")

    r = svc.set_relation
    for user in ("user:dev", "user:mod-owner", "user:mod-maint"):
        r(SYSTEM, PROJECT, "member", user)
    # agent:dev reads/writes project (so module + mr resolve) and its own
    # dev session; it is never enrolled org-wide.
    for scope in (PROJECT, DEVSESSION):
        r(SYSTEM, scope, "reader_agent", "agent:dev")
        r(SYSTEM, scope, "writer_agent", "agent:dev")
    # The two ADR-0011 confirmer tuples.
    r(SYSTEM, PAYMENTS, "module_owner", "user:mod-owner")
    r(SYSTEM, PROJECT, "maintainer", "user:mod-maint")

    # The ADR-0010 boundary mapping. 'src/*' → core, more-specific
    # 'src/payments/*' → payments, 'src/billing/*' → billing.
    svc.set_module_paths(SYSTEM, module_scope_id=CORE, globs=["src/*"])
    svc.set_module_paths(SYSTEM, module_scope_id=PAYMENTS, globs=["src/payments/*"])
    svc.set_module_paths(SYSTEM, module_scope_id=BILLING, globs=["src/billing/*"])
    return svc


# ---------- ADR-0010: path-glob boundary resolution ----------

def test_module_paths_most_specific_wins_unmatched_is_no_module(mod):
    svc = mod
    with svc.pool.connection() as conn:
        cur = conn.cursor()
        # Both 'src/*' and 'src/payments/*' match; the longest glob wins.
        assert scopes.modules_touched(cur, PROJECT, ["src/payments/charge.py"]) == [PAYMENTS]
        assert scopes.modules_touched(cur, PROJECT, ["src/util/log.py"]) == [CORE]
        # An unmatched path belongs to no module (falls through to project).
        assert scopes.modules_touched(cur, PROJECT, ["docs/readme.md"]) == []
        # Ordered, unique across several paths.
        assert scopes.modules_touched(
            cur, PROJECT,
            ["src/payments/a.py", "src/util/b.py", "src/payments/c.py", "docs/x.md"],
        ) == [PAYMENTS, CORE]


# ---------- ADR-0009: chain resolution with module scopes ----------

def test_chain_inserts_touched_modules_after_container_before_project(mod):
    svc = mod
    flow = Flow(surface="gitlab", container=MR, touched_paths=("src/payments/x.py",))
    with svc.pool.connection() as conn:
        chain = scopes.resolve_chain(conn.cursor(), DEV_AGENT, flow)
    # mr → touched module → project (org filtered: agent has no org enrollment).
    assert chain.index(MR) < chain.index(PAYMENTS) < chain.index(PROJECT)
    assert "org:acme" not in chain


def test_chain_filters_modules_by_readability(mod):
    svc = mod
    flow = Flow(surface="gitlab", container=MR, touched_paths=("src/payments/x.py",))
    with svc.pool.connection() as conn:
        cur = conn.cursor()
        # A member of the project sees the module; a non-member does not
        # (module goes through the standard container access path).
        seen = scopes.resolve_chain(cur, DEV_FOR_DEV, flow)
        hidden = scopes.resolve_chain(cur, DEV_FOR_OUT, flow)
    assert PAYMENTS in seen
    assert PAYMENTS not in hidden and MR not in hidden


def test_dev_time_flow_injects_the_project_after_the_modules(mod):
    svc = mod
    # A devsession is not under the project; the flow names the project and
    # the chain injects it right after the touched modules (ADR-0012).
    flow = Flow(surface="ide", container=DEVSESSION, project=PROJECT,
                touched_paths=("src/payments/x.py",))
    with svc.pool.connection() as conn:
        chain = scopes.resolve_chain(conn.cursor(), DEV_AGENT, flow)
    assert chain.index(DEVSESSION) < chain.index(PAYMENTS) < chain.index(PROJECT)


# ---------- ADR-0013: policy routing to module / agent scopes ----------

def _observe(svc, content, *, category, paths, kind="semantic"):
    flow = Flow(surface="ide", container=DEVSESSION, project=PROJECT,
                touched_paths=tuple(paths))
    return svc.remember(DEV_AGENT, flow, content=content, kind=kind,
                        origin_kind="agent_observed", categories=[category])


def test_route_module_single_match_lands_in_the_module(mod):
    svc = mod
    out = _observe(svc, "Payments always use integer minor units for money",
                   category="codebase_convention", paths=["src/payments/money.py"])
    assert out["decision"] == "stage"      # staged by default (ADR-0013)
    assert svc.get_memory(SYSTEM, out["memory_id"])["scope_id"] == PAYMENTS


def test_route_module_multi_match_falls_back_to_project(mod):
    svc = mod
    out = _observe(svc, "Every service logs structured JSON",
                   category="codebase_convention",
                   paths=["src/payments/a.py", "src/util/b.py"])
    # Two modules touched → ambiguous → the project scope (never a wrong one).
    assert svc.get_memory(SYSTEM, out["memory_id"])["scope_id"] == PROJECT


def test_route_module_no_match_falls_back_to_project(mod):
    svc = mod
    out = _observe(svc, "Repository-wide: run the formatter before pushing",
                   category="codebase_convention", paths=["docs/contributing.md"])
    assert svc.get_memory(SYSTEM, out["memory_id"])["scope_id"] == PROJECT


def test_route_agent_auto_provisions_the_agent_scope(mod):
    svc = mod
    with svc.pool.connection() as conn:  # the agent scope does not exist yet
        assert scopes.get_scope(conn.cursor(), "agent:dev") is None
    out = _observe(svc, "I grep the call sites before renaming a symbol",
                   category="task_tactic", paths=["src/payments/x.py"], kind="procedural")
    assert out["decision"] == "stage"
    # The agent's private scope was provisioned and the tactic landed there;
    # only the agent itself may read it (doc 07 §5), so check the row directly.
    with svc.pool.connection() as conn:
        assert scopes.get_scope(conn.cursor(), "agent:dev") is not None
        row = conn.cursor().execute(
            "SELECT scope_id FROM memories WHERE id=%s", (out["memory_id"],)
        ).fetchone()
    assert row["scope_id"] == "agent:dev"


def test_dev_observation_carries_a_trust_penalty(mod):
    svc = mod
    ep = svc.register_episode(
        DEV_AGENT, scope_id=DEVSESSION, source_kind="dev_observation",
        external_ref={"paths": ["src/payments/x.py"]},
        content="noticed the retry path", author="user:dev",
    )
    # A plain observation (no convention/tactic category) stays in its source
    # scope; its trust reflects the self-reported dev_observation source.
    out = svc.remember(
        DEV_AGENT, Flow(surface="ide", container=DEVSESSION, project=PROJECT),
        content="The payments retry path dedupes on charge id",
        kind="semantic", origin_kind="agent_observed", source_episode_ids=[ep["id"]],
    )
    assert out["decision"] == "stage"
    mem = svc.status(SYSTEM, memory_id=out["memory_id"])["memory"]
    assert mem["scope_id"] == DEVSESSION
    assert mem["trust_score"] == pytest.approx(0.45)


# ---------- ADR-0011: mr→module and module→project promotion ----------

def test_mr_to_module_asks_and_the_module_owner_confirms(mod):
    svc = mod
    mid = svc.remember(DEV_AGENT, MR_FLOW,
                       content="Payments retries are idempotent by charge id",
                       kind="semantic", origin_kind="explicit_user_ask")["memory_id"]
    out = svc.promote(DEV_AGENT, MR_FLOW, memory_id=mid, target_scope=PAYMENTS)
    assert out["decision"] == "ask"
    # A project member who is not the module owner cannot confirm.
    with pytest.raises(AccessDenied):
        svc.confirm_pending(Principal("user:dev"), out["pending_id"], approved=True)
    svc.confirm_pending(MOD_OWNER, out["pending_id"], approved=True)
    assert svc.get_memory(SYSTEM, mid)["scope_id"] == PAYMENTS


def test_module_to_project_asks_and_the_maintainer_confirms(mod):
    svc = mod
    mid = svc.remember(DEV_AGENT, MR_FLOW,
                       content="Modules expose health checks on /healthz",
                       kind="semantic", origin_kind="explicit_user_ask")["memory_id"]
    up1 = svc.promote(DEV_AGENT, MR_FLOW, memory_id=mid, target_scope=PAYMENTS)
    svc.confirm_pending(MOD_OWNER, up1["pending_id"], approved=True)

    # module → project is the stricter crossing: a repo-wide steward, not one
    # module's owner, confirms it.
    up2 = svc.promote(DEV_AGENT, Flow(surface="gitlab", container=PROJECT),
                      memory_id=mid, target_scope=PROJECT)
    assert up2["decision"] == "ask"
    with pytest.raises(AccessDenied):
        svc.confirm_pending(MOD_OWNER, up2["pending_id"], approved=True)
    svc.confirm_pending(MOD_MAINT, up2["pending_id"], approved=True)
    assert svc.get_memory(SYSTEM, mid)["scope_id"] == PROJECT


def test_promotion_tighten_keys_parse_and_apply():
    doc = policy.parse_document({
        "policy": "lockdown", "layer": "org", "applies_to": "org:acme",
        "promotion": {"to_module_scope": "deny", "to_project_scope": "deny"},
    })
    layer = policy.layer_from_document(doc, 1)
    for crossing in ("module", "module_project"):
        gate = policy.promotion_gate([layer], crossing=crossing,
                                     source_trust_class="internal_public")
        assert gate.decision == "deny"
    # The keys only tighten — 'allow' is rejected like every promotion key.
    with pytest.raises(policy.PolicyError):
        policy.parse_document({
            "policy": "bad", "layer": "org", "applies_to": "org:acme",
            "promotion": {"to_module_scope": "allow"},
        })


# ---------- ADR-0012: the module hint and dev-time enrollment ----------

def test_staged_queue_exposes_the_module_hint(mod):
    svc = mod
    # A candidate learned narrow in the MR, carrying the paths it touched.
    svc.remember(DEV_AGENT, Flow(surface="gitlab", container=MR,
                                 touched_paths=("src/payments/refund.py",)),
                 content="Refunds re-use the original charge id",
                 kind="semantic", origin_kind="llm_inferred")
    queue = svc.staged_queue(MOD_MAINT, scope_id=MR)
    item = next(q for q in queue if q["content"] == "Refunds re-use the original charge id")
    assert item["modules_touched"] == [PAYMENTS]


def test_memory_observe_requires_writer_enrollment(mod):
    svc = mod
    # An enrolled dev agent may self-report an episode (the memory_observe path).
    ep = svc.register_episode(
        DEV_AGENT, scope_id=DEVSESSION, source_kind="dev_observation",
        external_ref={"paths": ["src/payments/x.py"]}, content="observed", author="user:dev",
    )
    assert ep["source_kind"] == "dev_observation"
    # An agent with no writer enrollment is refused.
    with pytest.raises(AccessDenied):
        svc.register_episode(Principal("agent:intruder"), scope_id=DEVSESSION,
                             source_kind="dev_observation", external_ref={}, content="nope")
    # Platform ingestion (system) keeps its unrestricted reach.
    ok = svc.register_episode(SYSTEM, scope_id=DEVSESSION, source_kind="mr_comment",
                              external_ref={}, content="platform-side")
    assert ok["id"]


# ---------- ADR-0010: module-path administration ----------

def test_module_paths_admin_gate(mod):
    svc = mod
    admin_module = "module:mod-api/admintest"
    svc.create_scope(SYSTEM, scope_id=admin_module, family="module", parent_scope_id=PROJECT)
    client = TestClient(create_app(service=svc))

    denied = client.post("/v1/module-paths", headers={"X-Memoramum-Actor": "agent:dev"},
                         json={"module_scope_id": admin_module, "globs": ["src/admin/*"]})
    assert denied.status_code == 403

    ok = client.post("/v1/module-paths", headers={"X-Memoramum-Actor": "user:root"},
                     json={"module_scope_id": admin_module, "globs": ["src/admin/*"]})
    assert ok.status_code == 200 and ok.json()["globs"] == ["src/admin/*"]
    with svc.pool.connection() as conn:
        assert scopes.modules_touched(conn.cursor(), PROJECT, ["src/admin/roles.py"]) == [admin_module]


# ---------- issue #17: consolidator isolates sibling modules ----------

def test_consolidator_does_not_compare_sibling_modules(pool, jobs_isolated):
    """Contradicting conventions in two sibling module scopes are never
    merged or flagged against each other — every consolidation job is per
    scope (ADR-0009)."""
    svc = jobs_isolated
    a = _observe(svc, "Money amounts are rounded up to the nearest cent",
                 category="codebase_convention", paths=["src/payments/round.py"])
    b = _observe(svc, "Money amounts are rounded down to the nearest cent",
                 category="codebase_convention", paths=["src/billing/round.py"])
    assert svc.get_memory(SYSTEM, a["memory_id"])["scope_id"] == PAYMENTS
    assert svc.get_memory(SYSTEM, b["memory_id"])["scope_id"] == BILLING
    # The write of b (billing chain) never saw a (payments): no contradiction.
    assert "contradicts" not in b and "supersedes" not in b

    from memoramum.consolidator import Consolidator
    merged = Consolidator(svc).dedupe(NOW)
    assert not any({a["memory_id"], b["memory_id"]} <= set(m["merged"]) for m in merged)
    # And nothing queued them as a contradicting pair.
    pairs = {(str(q["challenger_id"]), str(q["contradicted_id"]))
             for q in svc.contradictions(SYSTEM, include_resolved=True)}
    assert (a["memory_id"], b["memory_id"]) not in pairs
    assert (b["memory_id"], a["memory_id"]) not in pairs


@pytest.fixture()
def jobs_isolated(pool, mod):
    # A contradiction-capable judge, to prove the isolation is structural
    # (per-scope) and not merely the exact judge staying silent.
    return MemoryService(pool, Settings(database_url=TEST_DB, embedder="hash", judge="overlap"))
