"""Recall and the source-visibility invariant (doc 01 §3.2 rule 4,
doc 05 §4.1): membership checked at retrieval time, intersection rule,
no Slack scopes in Marge's chain."""

from conftest import (
    ADMIN, DEPLOYS_FLOW, MARGE_FOR_DANA, MR_FLOW, SAGE_FOR_DANA, SAGE_FOR_EVE,
    SAGE_FOR_LI, SYSTEM,
)

import pytest

from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied


@pytest.fixture(scope="module")
def channel_memory(svc):
    return svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="The Atlas deploy window is announced in the deploys channel",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]


def test_recall_in_channel_emits_read_event(svc, channel_memory):
    hits = svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="Atlas deploy window")
    assert channel_memory in [h["id"] for h in hits]
    hit = next(h for h in hits if h["id"] == channel_memory)
    assert "remembered at user request in #deploys" in hit["provenance"]

    reads = svc.audit_events(ADMIN, action="READ", actor="agent:sage")
    assert any(channel_memory in e["details"]["memory_ids"] for e in reads)
    assert svc.get_memory(SAGE_FOR_DANA, channel_memory)["access_count"] >= 1


def test_channel_memory_never_reaches_marge(svc, channel_memory):
    """Marge's chain contains no Slack scopes (doc 01 §3.3)."""
    hits = svc.recall(MARGE_FOR_DANA, MR_FLOW, query="Atlas deploy window")
    assert channel_memory not in [h["id"] for h in hits]


def test_org_scope_memory_crosses_surfaces(svc):
    """Cross-surface sharing falls out of scope placement, not a mechanism."""
    org_flow = Flow(surface="slack", container="org:acme", session_id="sess-3")
    mid = svc.remember(
        Principal("user:dana"), org_flow,
        content="Production deploys are frozen during the December change freeze",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]
    hits = svc.recall(MARGE_FOR_DANA, MR_FLOW, query="production deploys frozen change freeze")
    assert mid in [h["id"] for h in hits]


def test_intersection_rule_blocks_non_member_user(svc, channel_memory):
    """Sage is enrolled in the channel, but eve isn't a member: the
    on-behalf-of user's visibility bounds the read."""
    hits = svc.recall(SAGE_FOR_EVE, DEPLOYS_FLOW, query="Atlas deploy window")
    assert channel_memory not in [h["id"] for h in hits]


def test_leaving_the_channel_revokes_immediately(svc, channel_memory):
    hits = svc.recall(SAGE_FOR_LI, DEPLOYS_FLOW, query="Atlas deploy window")
    assert channel_memory in [h["id"] for h in hits]
    svc.set_relation(SYSTEM, "channel/C0DEP", "member", "user:li", remove=True)
    try:
        hits = svc.recall(SAGE_FOR_LI, DEPLOYS_FLOW, query="Atlas deploy window")
        assert channel_memory not in [h["id"] for h in hits]
    finally:
        svc.set_relation(SYSTEM, "channel/C0DEP", "member", "user:li")


def test_subject_scope_only_unlocked_by_the_subject(svc):
    dana_flow = Flow(surface="slack", container="channel/C0DEP",
                     participants=("user:dana",), session_id="s")
    mid = svc.remember(
        SAGE_FOR_DANA, Flow(surface="slack", container="subject:user/dana", session_id="s"),
        content="dana prefers deploy summaries as threads, not channel messages",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["user:dana"], categories=["preference"],
    )["memory_id"]
    assert mid in [h["id"] for h in svc.recall(SAGE_FOR_DANA, dana_flow, query="deploy summaries threads")]
    # li's session lists dana as participant, but that does not unlock
    # dana's subject scope for li (doc 05 §4.1, P1 posture).
    li_flow = Flow(surface="slack", container="channel/C0DEP",
                   participants=("user:li", "user:dana"), session_id="s")
    assert mid not in [h["id"] for h in svc.recall(SAGE_FOR_LI, li_flow, query="deploy summaries threads")]


def test_private_scope_with_no_membership_fails_closed(svc):
    from memoramum.scopes import user_can_see
    with svc.pool.connection() as conn:
        cur = conn.cursor()
        from memoramum.scopes import create_scope
        create_scope(cur, scope_id="dm/D999", family="container",
                     parent_scope_id="workspace/T024B", trust_class="private")
        assert user_can_see(cur, "user:dana", "dm/D999") is False


def test_as_of_requires_auditor(svc):
    from datetime import datetime, timezone
    with pytest.raises(AccessDenied):
        svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="anything",
                   as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
