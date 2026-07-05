"""P1 exit criteria (doc 07 §6): worked-scenario step 1 (explicit variant)
and step 6, end to end — plus the subject-scope hash chain."""

from conftest import ADMIN, DEPLOYS_FLOW, SAGE_FOR_DANA, SYSTEM

import pytest

from memoramum import events
from memoramum.principals import Flow
from memoramum.service import AccessDenied


def test_scenario_step1_explicit_and_step6_audit(svc):
    # Step 1 (explicit variant): dana says it, dana asks Sage to remember it.
    episode = svc.register_episode(
        SYSTEM, scope_id="channel/C0DEP", source_kind="slack_message",
        external_ref={"channel": "C0DEP", "ts": "1709481600.123"},
        content="reminder: Team Atlas deploys to production only on Tuesdays",
        author="user:dana",
    )
    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Team Atlas deploys to production only on Tuesdays.",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["team:atlas"], categories=["process", "schedule"],
        justification="dana asked Sage to keep the standing deploy rule",
        source_episode_ids=[episode["id"]],
    )
    assert verdict["decision"] == "allow" and verdict["status"] == "active"
    mid = verdict["memory_id"]

    # An agent answer built on it → a READ event.
    hits = svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="when does team atlas deploy")
    assert mid in [h["id"] for h in hits]

    # Step 6: "why did the bot say that?" — one traversal away, via the
    # audit surfaces only.
    read = next(e for e in svc.audit_events(ADMIN, action="READ", actor="agent:sage")
                if mid in e["details"]["memory_ids"])
    assert read["on_behalf_of"] == "user:dana"

    history = svc.memory_history(ADMIN, mid)
    assert [e["action"] for e in history if e["action"] == "PROPOSE"]

    status = svc.status(ADMIN, memory_id=mid)
    assert status["provenance"]["origin_kind"] == "explicit_user_ask"
    source = status["sources"][0]
    assert source["source_type"] == "episode"

    ep = svc.get_episode(ADMIN, source["source_id"])
    assert ep["external_ref"]["ts"] == "1709481600.123"       # dana's exact message
    assert ep["author"] == "user:dana"

    # The policy decision that allowed the write is linked from provenance.
    decision = next(e for e in svc.audit_events(ADMIN, action="POLICY_DECISION")
                    if e["details"].get("rule_id", "").startswith("org/explicit-asks"))
    assert decision["details"]["verdict"] == "allow"


def test_subject_scope_events_are_hash_chained_and_tamper_evident(svc):
    svc.remember(
        SAGE_FOR_DANA,
        Flow(surface="slack", container="subject:user/dana", session_id="s"),
        content="dana's timezone is Europe/Amsterdam",
        kind="profile", origin_kind="explicit_user_ask", subjects=["user:dana"],
    )
    with svc.pool.connection() as conn:
        cur = conn.cursor()
        assert events.verify_chain(cur, "subject:user/dana") is True
        cur.execute(
            "UPDATE memory_events SET details = details || '{\"tampered\": true}'::jsonb"
            " WHERE scope_id='subject:user/dana' AND curr_hash IS NOT NULL"
            " AND seq = (SELECT min(seq) FROM memory_events"
            "            WHERE scope_id='subject:user/dana' AND curr_hash IS NOT NULL)"
        )
        assert events.verify_chain(cur, "subject:user/dana") is False
        conn.rollback()


def test_event_log_queries_require_auditor(svc):
    with pytest.raises(AccessDenied):
        svc.audit_events(SAGE_FOR_DANA)
