"""The `ask` verdict as a first-class two-step (doc 04 §1, doc 05 §2):
the tool returns the exact question, the human's answer comes back via
memory_confirm, and CONFIRM/REJECT are the human's events. Uses a
dedicated agent + scope + policy so the stored policy stays contained."""

from conftest import ADMIN, SYSTEM

import pytest

from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

ROOT = Principal("user:root")
DANA = Principal("user:dana")
LI = Principal("user:li")
ASKY_FOR_DANA = Principal("agent:asky", "user:dana")

ASK_FLOW = Flow(surface="slack", container="channel/C0ASK",
                participants=("user:dana", "user:li"), session_id="sess-ask")

ASKY_POLICY = {
    "policy": "asky-default", "layer": "agent", "applies_to": "agent:asky",
    "learning": {"strategies": [
        {"name": "sensitive-personal", "categories": ["health"],
         "decision": "ask", "sensitivity_floor": "confidential",
         "reason": "never silent, regardless of origin"},
        {"name": "explicit-asks", "origin_kinds": ["explicit_user_ask"], "decision": "allow"},
    ]},
}


@pytest.fixture(scope="module", autouse=True)
def ask_world(svc):
    svc.create_scope(SYSTEM, scope_id="channel/C0ASK", family="container",
                     parent_scope_id="workspace/T024B", surface="slack",
                     external_ref={"name": "#ask-lab"})
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, "channel/C0ASK", "member", user)
    svc.set_relation(SYSTEM, "channel/C0ASK", "reader_agent", "agent:asky")
    svc.set_relation(SYSTEM, "channel/C0ASK", "writer_agent", "agent:asky")
    svc.put_policy(ROOT, ASKY_POLICY)


def _propose(svc, content="li is on medical leave until March"):
    return svc.remember(
        ASKY_FOR_DANA, ASK_FLOW, content=content,
        kind="semantic", origin_kind="llm_inferred",
        subjects=["user:li"], categories=["health"],
    )


def test_ask_returns_the_question_and_stores_nothing(svc):
    out = _propose(svc)
    assert out["decision"] == "ask" and out["memory_id"] is None
    assert out["ask_prompt"].startswith("Want me to remember:")
    assert out["pending_id"]
    assert out["rule_id"].startswith("agent/sensitive-personal")
    listed = svc.pending_queue(DANA)
    assert out["pending_id"] in [p["id"] for p in listed]


def test_decline_rejects_and_stores_nothing(svc):
    """User declines → REJECT event, nothing stored (doc 05 §6)."""
    out = _propose(svc, "li sprained an ankle bouldering")
    declined = svc.confirm_pending(ASKY_FOR_DANA, out["pending_id"], approved=False,
                                   note="not our business")
    assert declined["approved"] is False and declined["memory_id"] is None
    scope = svc.status(DANA, scope="channel/C0ASK")
    assert all("ankle" not in m["content"] for m in scope["memories"])
    rejects = svc.audit_events(ADMIN, action="REJECT", actor="user:dana")
    assert any(e["details"].get("pending_id") == out["pending_id"] for e in rejects)
    with pytest.raises(ValueError):
        svc.confirm_pending(DANA, out["pending_id"], approved=True)  # already resolved


def test_approve_enacts_with_the_humans_confirmation(svc):
    out = _propose(svc, "li is out for surgery recovery in April")
    confirmed = svc.confirm_pending(ASKY_FOR_DANA, out["pending_id"], approved=True)
    assert confirmed["approved"] is True and confirmed["memory_id"]
    mem = svc.get_memory(ASKY_FOR_DANA, confirmed["memory_id"])
    # Explicit confirmation earns active (doc 03 §4) and the ask's
    # sensitivity floor stuck (doc 05 §1).
    assert mem["status"] == "active"
    assert mem["sensitivity"] == "confidential"
    history = svc.memory_history(ADMIN, confirmed["memory_id"])
    confirm = next(e for e in history if e["action"] == "CONFIRM")
    assert confirm["actor"] == "user:dana"           # the human's decision (doc 01 §4)
    assert confirm["details"]["relayed_by"] == "agent:asky"
    assert {"PROPOSE", "CONFIRM", "PROMOTE_STATUS"} <= {e["action"] for e in history}


def test_only_the_asked_user_answers(svc):
    out = _propose(svc, "li will be at a health checkup on Friday")
    with pytest.raises(AccessDenied):
        svc.confirm_pending(LI, out["pending_id"], approved=True)
    with pytest.raises(AccessDenied):   # agents alone are not confirmers
        svc.confirm_pending(Principal("agent:asky"), out["pending_id"], approved=True)
    svc.confirm_pending(DANA, out["pending_id"], approved=False)
