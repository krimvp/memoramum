"""memory_forget (doc 04 §1) — hard-forget path 1 (doc 03 §6): agents
have narrow forget powers, everything else asks; users archive by
default, and subject-scope memories tombstone on explicit user request."""

from conftest import DANA, SYSTEM

import pytest

from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

SAGE = Principal("agent:sage")
SAGE_FOR_DANA = Principal("agent:sage", "user:dana")
CHANNEL = "channel/C0FORG"
FLOW = Flow(surface="slack", container=CHANNEL,
            participants=("user:dana", "user:li"), session_id="s-forg")


@pytest.fixture(scope="module", autouse=True)
def world(svc):
    svc.create_scope(SYSTEM, scope_id=CHANNEL, family="container",
                     parent_scope_id="workspace/T024B", surface="slack")
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, CHANNEL, "member", user)
    svc.set_relation(SYSTEM, CHANNEL, "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, CHANNEL, "writer_agent", "agent:sage")
    return svc


def test_agent_forgets_in_its_own_notebook(svc):
    flow = Flow(surface="slack", container="agent:sage", session_id="s-own")
    mid = svc.remember(
        SAGE, flow, content="Scratch note: the channel prefers threads",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    out = svc.forget(SAGE, flow, memory_id=mid, reason="scratch")
    assert out["decision"] == "allow" and out["status"] == "archived"
    assert out["rule_id"] == "forget/own-authority"
    history = {e["action"] for e in svc.memory_history(SAGE, mid)}
    assert "FORGET" in history


def test_agent_forgets_its_own_staged_proposal(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, FLOW, content="Atlas might drop the canary stage",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    out = svc.forget(SAGE, FLOW, memory_id=mid, reason="retracted mid-conversation")
    assert out["decision"] == "allow" and out["status"] == "archived"


def test_anything_else_routes_to_ask(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, FLOW, content="Deploy calendars sync from the shared drive",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    out = svc.forget(SAGE, FLOW, memory_id=mid, reason="looks stale")
    assert out["decision"] == "ask" and out["pending_id"]
    assert svc.get_memory(SYSTEM, mid)["status"] == "active"    # nothing moved yet

    confirmed = svc.confirm_pending(DANA, out["pending_id"], approved=True)
    assert confirmed["action"] == "forget" and confirmed["status"] == "archived"
    history = [e["action"] for e in svc.memory_history(SYSTEM, mid)]
    assert "CONFIRM" in history and "FORGET" in history
    confirm = next(e for e in svc.memory_history(SYSTEM, mid) if e["action"] == "CONFIRM")
    assert confirm["actor"] == "user:dana"      # the decision recorded is the human's


def test_user_forget_in_subject_scope_always_tombstones(svc):
    flow = Flow(surface="slack", container="subject:user/dana", session_id="s-subj")
    mid = svc.remember(
        DANA, flow, content="dana's old desk phone was 555-0100",
        kind="semantic", origin_kind="explicit_user_ask", subjects=["user:dana"],
    )["memory_id"]
    out = svc.forget(DANA, flow, memory_id=mid, reason="forget that about me",
                     mode="archive")     # archive requested…
    assert out["status"] == "tombstoned"  # …but subject scopes tombstone (doc 03 §6)
    row = svc.status(SYSTEM, memory_id=mid)["memory"]
    assert row["status"] == "tombstoned" and row["content"] == ""
    history = {e["action"] for e in svc.memory_history(SYSTEM, mid)}
    assert {"FORGET", "TOMBSTONE"} <= history


def test_a_bystander_cannot_forget(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, FLOW, content="Runbooks live in the platform wiki",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    with pytest.raises(AccessDenied):
        svc.forget(Principal("user:eve"), FLOW, memory_id=mid)
