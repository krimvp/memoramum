"""Scope promotion (doc 03 §4, doc 05 §3) and the P3 exit criteria of
doc 07 §6: worked-scenario steps 3–4 across two scopes — dana confirms
the channel → workspace crossing, and Marge retrieves the promoted
memory cross-surface in MR !482."""

from conftest import (
    ADMIN, DANA, DEPLOYS_FLOW, MARGE_FOR_DANA, MR_FLOW, SAGE_FOR_DANA, SYSTEM,
)

import pytest

from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

LI = Principal("user:li")
EVE = Principal("user:eve")


def test_scenario_steps_3_and_4_promotion_then_cross_surface_recall(svc):
    # The Tuesday rule is active in #deploys (steps 1–2 are the P1/P2
    # exit criteria; the explicit variant lands active directly).
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Atlas production deploys happen only on Tuesdays",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
        subjects=["team:atlas"],
    )["memory_id"]

    # Step 3: "make sure the other teams know this too" — a shared-scope
    # crossing returns ask (doc 05 §3), confirmable by a source-scope member.
    out = svc.promote(
        SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=mid, target_scope="workspace/T024B",
        justification="dana asked to share the deploy rule with all teams",
    )
    assert out["decision"] == "ask" and out["pending_id"]
    assert "workspace" in out["ask_prompt"] or "T024B" in out["ask_prompt"]
    assert svc.get_memory(SAGE_FOR_DANA, mid)["scope_id"] == "channel/C0DEP"  # nothing moved yet

    # eve is a workspace member but not a #deploys member: not a confirmer.
    with pytest.raises(AccessDenied):
        svc.confirm_pending(EVE, out["pending_id"], approved=True)

    # dana (scope member) confirms → audited promotion.
    confirmed = svc.confirm_pending(DANA, out["pending_id"], approved=True)
    assert confirmed["approved"] is True and confirmed["to"] == "workspace/T024B"
    assert svc.get_memory(SAGE_FOR_DANA, mid)["scope_id"] == "workspace/T024B"

    history = {e["action"] for e in svc.memory_history(SAGE_FOR_DANA, mid)}
    assert {"CONFIRM", "PROMOTE_SCOPE"} <= history
    confirm = next(e for e in svc.memory_history(SAGE_FOR_DANA, mid)
                   if e["action"] == "CONFIRM")
    assert confirm["actor"] == "user:dana"  # the confirmation is the human's (doc 05 §3)
    promote_scope = next(e for e in svc.memory_history(SAGE_FOR_DANA, mid)
                         if e["action"] == "PROMOTE_SCOPE")
    assert promote_scope["details"]["from"] == "channel/C0DEP"
    assert promote_scope["details"]["policy_decision_id"]  # the full chain (doc 05 §3)
    status = svc.status(ADMIN, memory_id=mid)
    assert status["provenance"] is not None

    # Step 4: Marge is enrolled in the shared workspace scope (README step
    # 4: "relevant shared scopes it is enrolled in"; doc 01 §5) and
    # retrieves the memory from the MR flow — cross-surface recall falls
    # out of scope placement.
    svc.set_relation(SYSTEM, "workspace/T024B", "reader_agent", "agent:marge")
    hits = svc.recall(MARGE_FOR_DANA, MR_FLOW, query="when do Atlas production deploys happen")
    assert mid in [h["id"] for h in hits]
    reads = svc.audit_events(ADMIN, action="READ", actor="agent:marge", scope_id="mr/482")
    assert any(mid in e["details"]["memory_ids"] for e in reads)


def test_promotion_declined_leaves_a_reject_event(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy dashboards are linked from the channel bookmarks",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]
    out = svc.promote(SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=mid,
                      target_scope="workspace/T024B")
    declined = svc.confirm_pending(DANA, out["pending_id"], approved=False,
                                   note="channel-specific, keep it here")
    assert declined["approved"] is False
    assert svc.get_memory(SAGE_FOR_DANA, mid)["scope_id"] == "channel/C0DEP"
    assert any(e["action"] == "REJECT" for e in svc.memory_history(SAGE_FOR_DANA, mid))


def test_subject_crossing_asks_the_subject(svc):
    """container → subject: dana confirms what is recorded about dana in
    shared view (doc 05 §3) — a bystander member cannot."""
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="dana coordinates the Atlas release calendar",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["user:dana"], categories=["process"],
    )["memory_id"]
    out = svc.promote(SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=mid,
                      target_scope="subject:user/dana")
    assert out["decision"] == "ask"
    with pytest.raises(AccessDenied):
        svc.confirm_pending(LI, out["pending_id"], approved=True)
    svc.confirm_pending(DANA, out["pending_id"], approved=True)
    assert svc.get_memory(SAGE_FOR_DANA, mid)["scope_id"] == "subject:user/dana"


def test_nothing_leaves_a_private_scope(svc):
    mid = svc.remember(
        DANA, Flow(surface="slack", container="dm/D111", session_id="s-dm"),
        content="The postmortem draft lives in dana's DM with the bot",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    out = svc.promote(DANA, Flow(surface="slack", container="dm/D111"),
                      memory_id=mid, target_scope="workspace/T024B")
    assert out["decision"] == "deny"
    assert out["rule_id"] == "org/promotion-private-floor"


def test_broader_to_narrower_needs_no_ceremony(svc):
    mid = svc.remember(
        DANA, Flow(surface="slack", container="workspace/T024B", session_id="s-ws"),
        content="Workspace-wide demos happen on the last Thursday",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    out = svc.promote(DANA, Flow(surface="slack", container="workspace/T024B"),
                      memory_id=mid, target_scope="channel/C0DEP")
    assert out["decision"] == "allow" and out["pending_id"] is None
    assert svc.get_memory(DANA, mid)["scope_id"] == "channel/C0DEP"


def test_the_target_must_be_on_the_writable_set(svc):
    """The one tool where the agent names a scope (doc 04 §1)."""
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy announcements are mirrored to the status page",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    out = svc.promote(SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=mid,
                      target_scope="project/platform-api")
    assert out["decision"] == "deny" and out["rule_id"] == "access/enrollment"
