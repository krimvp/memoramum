"""The incident levers of doc 06 §3: lineage quarantine (source predicate
→ reverse derivation walk → mass QUARANTINE → review) and the break-glass
per-agent write-freeze."""

from datetime import datetime, timezone

from conftest import ADMIN, SYSTEM

import pytest

from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

ROOT = Principal("user:root")     # org admin (owner on org:acme)
SAGE_FOR_DANA = Principal("agent:sage", "user:dana")
CHANNEL = "channel/C0QUAR"
FLOW = Flow(surface="slack", container=CHANNEL,
            participants=("user:dana", "user:li"), session_id="s-quar")


@pytest.fixture(scope="module")
def poisoned(svc):
    """eve seeds two plausible 'facts'; one is consolidated onward —
    the derivation walk must catch the derivative too."""
    svc.create_scope(SYSTEM, scope_id=CHANNEL, family="container",
                     parent_scope_id="workspace/T024B", surface="slack")
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, CHANNEL, "member", user)
    svc.set_relation(SYSTEM, CHANNEL, "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, CHANNEL, "writer_agent", "agent:sage")

    eps = [
        svc.register_episode(
            SYSTEM, scope_id=CHANNEL, source_kind="slack_message",
            external_ref={"channel": "C0QUAR", "ts": f"poison-{i}"},
            content=text, author="user:eve",
            occurred_at=datetime.now(timezone.utc),
        )
        for i, text in enumerate([
            "the deploy approver list now includes contractors",
            "canary checks can be skipped for hotfixes",
        ])
    ]
    mids = [
        svc.remember(
            SAGE_FOR_DANA, FLOW, content=ep["content"], kind="semantic",
            origin_kind="llm_inferred", source_episode_ids=[ep["id"]],
        )["memory_id"]
        for ep in eps
    ]
    derived = svc.remember(
        Principal("system:consolidator"), FLOW,
        content="Deploy gating is looser than documented",
        kind="semantic", origin_kind="consolidated", source_memory_ids=mids,
    )["memory_id"]
    return {"episodes": eps, "memories": mids, "derived": derived}


def test_quarantine_by_author_walks_the_derivation_graph(svc, poisoned):
    hits = svc.recall(SAGE_FOR_DANA, FLOW, query="deploy approver contractors")
    assert poisoned["memories"][0] in [h["id"] for h in hits]   # live before

    out = svc.quarantine(ADMIN, author="user:eve", note="suspected poisoning")
    assert set(poisoned["memories"]) | {poisoned["derived"]} <= set(out["memory_ids"])

    # Excluded from ALL retrieval pending review (doc 06 §3).
    hits = svc.recall(SAGE_FOR_DANA, FLOW, query="deploy approver contractors")
    assert not set(poisoned["memories"]) & {h["id"] for h in hits}
    events = svc.audit_events(ADMIN, action="QUARANTINE",
                              memory_id=poisoned["derived"])
    assert events and events[0]["details"]["predicate"] == {"author": "user:eve"}

    # Review: restore one, tombstone another.
    queue = svc.quarantine_queue(ADMIN)
    assert {q["memory_id"] for q in queue} >= set(out["memory_ids"])
    svc.resolve_quarantine(ADMIN, out["request_id"], poisoned["memories"][0],
                           action="restore", note="checked with the team: true")
    hits = svc.recall(SAGE_FOR_DANA, FLOW, query="deploy approver contractors")
    assert poisoned["memories"][0] in [h["id"] for h in hits]

    svc.resolve_quarantine(ADMIN, out["request_id"], poisoned["memories"][1],
                           action="tombstone", note="fabricated")
    row = svc.status(ADMIN, memory_id=poisoned["memories"][1])["memory"]
    assert row["status"] == "tombstoned" and row["content"] == ""


def test_quarantine_is_an_incident_role_power(svc):
    with pytest.raises(AccessDenied):
        svc.quarantine(SAGE_FOR_DANA, author="user:eve")
    with pytest.raises(AccessDenied):
        svc.quarantine_queue(SAGE_FOR_DANA)


def test_break_glass_freezes_writes_not_reads(svc, poisoned):
    out = svc.set_agent_freeze(ROOT, "agent:sage", frozen=True, reason="incident 42")
    assert out == {"agent": "agent:sage", "frozen": True}
    try:
        verdict = svc.remember(
            SAGE_FOR_DANA, FLOW, content="Frozen agents write nothing",
            kind="semantic", origin_kind="explicit_user_ask",
        )
        assert verdict["decision"] == "deny"
        assert verdict["rule_id"] == "org/break-glass"
        # Reads are unaffected (doc 05 §5).
        assert svc.recall(SAGE_FOR_DANA, FLOW, query="deploy") is not None
    finally:
        svc.set_agent_freeze(ROOT, "agent:sage", frozen=False, reason="incident closed")

    verdict = svc.remember(
        SAGE_FOR_DANA, FLOW, content="Thawed agents write again",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    assert verdict["decision"] == "allow"
    changes = svc.audit_events(ADMIN, action="POLICY_CHANGE", actor="user:root")
    assert [c["details"]["frozen"] for c in changes[:2]] == [False, True]  # newest first
