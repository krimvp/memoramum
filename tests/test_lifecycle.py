"""P2 exit criterion 1 (doc 07 §6): staged→active promotions happening
organically — via re-observation, useful retrievals, and explicit
confirmation — plus the doc 03 §5 storage-side sweeps."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import ADMIN, DANA, SAGE_FOR_DANA, SYSTEM

from memoramum.consolidator import Consolidator
from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

LIFE_FLOW = Flow(surface="slack", container="channel/C0LIFE",
                 participants=("user:dana", "user:li"), session_id="sess-life")


@pytest.fixture(scope="module", autouse=True)
def life_world(svc):
    """A dedicated channel so lifecycle transitions don't leak into the
    shared worked-scenario world."""
    svc.create_scope(SYSTEM, scope_id="channel/C0LIFE", family="container",
                     parent_scope_id="workspace/T024B", surface="slack",
                     external_ref={"name": "#lifecycle"})
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, "channel/C0LIFE", "member", user)
    svc.set_relation(SYSTEM, "channel/C0LIFE", "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, "channel/C0LIFE", "writer_agent", "agent:sage")


def _episode(svc, author, content, **kw):
    return svc.register_episode(
        SYSTEM, scope_id="channel/C0LIFE", source_kind="slack_message",
        external_ref={"channel": "C0LIFE"}, content=content, author=author, **kw,
    )["id"]


def test_reobservation_reinforces_and_promotes(svc):
    """Scenario step 2, staged leg: PROPOSE → REINFORCE(re-obs, S=3.0) →
    PROMOTE_STATUS, exactly the doc 03 §7 event sequence."""
    ep1 = _episode(svc, "user:dana", "atlas retros happen the first monday of the month")
    first = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW,
        content="Atlas retros happen on the first Monday of the month",
        kind="semantic", origin_kind="llm_inferred", source_episode_ids=[ep1],
        justification="stated by dana",
    )
    assert first["decision"] == "stage" and first["status"] == "staged"
    mid = first["memory_id"]

    # An independent episode (different author) restates the fact.
    ep2 = _episode(svc, "user:li", "reminder: retro is first monday, as always")
    second = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW,
        content="Atlas retros happen on the first Monday of the month",
        kind="semantic", origin_kind="llm_inferred", source_episode_ids=[ep2],
        justification="restated by li",
    )
    assert second["deduplicated"] is True
    assert second["memory_id"] == mid                 # reinforced, not copied
    assert second["status"] == "active"               # re-observations ≥ 1 → promoted

    mem = svc.status(ADMIN, memory_id=mid)["memory"]
    assert mem["strength"] == 3.0                     # 1.0 + 2 (doc 03 §5, §7)
    actions = [e["action"] for e in svc.memory_history(ADMIN, mid)]
    assert actions.index("REINFORCE") < actions.index("PROMOTE_STATUS")
    promote = next(e for e in svc.memory_history(ADMIN, mid) if e["action"] == "PROMOTE_STATUS")
    assert promote["actor"] == "system:consolidator"  # the micro-run (doc 07 §2)
    # Reinforcement events name their episodes (doc 06 §3).
    reinforce = next(e for e in svc.memory_history(ADMIN, mid) if e["action"] == "REINFORCE")
    assert reinforce["details"]["episodes"] == [ep2]


def test_duplicate_without_independent_evidence_is_a_noop(svc):
    ep = _episode(svc, "user:dana", "deploy dashboards live in grafana")
    first = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Deploy dashboards live in Grafana",
        kind="semantic", origin_kind="llm_inferred", source_episode_ids=[ep],
    )
    # Same claim again with no independent evidence: nothing changes.
    again = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Deploy dashboards live in Grafana",
        kind="semantic", origin_kind="llm_inferred",
    )
    assert again["deduplicated"] is True
    assert again["memory_id"] == first["memory_id"]
    assert again["status"] == "staged"
    assert svc.status(ADMIN, memory_id=first["memory_id"])["memory"]["strength"] == 1.0


def test_useful_retrievals_promote_at_two(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Post-incident writeups are due within five days",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    one = svc.reinforce(SAGE_FOR_DANA, LIFE_FLOW, memory_id=mid, signal="useful")
    assert one["status"] == "staged"                  # 1 useful retrieval < 2
    two = svc.reinforce(SAGE_FOR_DANA, LIFE_FLOW, memory_id=mid, signal="useful",
                        note="used to answer the writeup-deadline question")
    assert two["status"] == "active"                  # useful retrievals ≥ 2 (doc 03 §4)
    assert two["strength"] == 3.0                     # 1.0 + 1 + 1


def test_wrong_signal_lowers_confidence_and_queues_review(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Staging credentials rotate on Fridays",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    out = svc.reinforce(SAGE_FOR_DANA, LIFE_FLOW, memory_id=mid, signal="wrong",
                        note="the rotation is actually automated, no weekday")
    assert out["confidence"] == pytest.approx(0.5)    # 0.7 - 0.2
    assert out["status"] == "staged"                  # no forget powers (doc 04 §1)
    queued = [q for q in svc.contradictions(ADMIN) if q["contradicted_id"] == mid]
    assert queued and queued[0]["challenger_id"] is None
    # An unresolved contradiction blocks rule-based promotion.
    svc.reinforce(SAGE_FOR_DANA, LIFE_FLOW, memory_id=mid, signal="useful")
    svc.reinforce(SAGE_FOR_DANA, LIFE_FLOW, memory_id=mid, signal="useful")
    assert svc.status(ADMIN, memory_id=mid)["memory"]["status"] == "staged"


def test_review_confirm_promotes_and_reject_archives(svc):
    confirm_id = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Atlas standups moved to 9:30",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    reject_id = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Atlas is switching to a monorepo next quarter",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]

    # The triage queue is scoped to what the reviewing human can see.
    queue = svc.staged_queue(DANA, scope_id="channel/C0LIFE")
    assert {confirm_id, reject_id} <= {m["id"] for m in queue}

    out = svc.review_staged(DANA, confirm_id, action="confirm", note="dana vouches")
    assert out["status"] == "active"
    history = svc.memory_history(ADMIN, confirm_id)
    confirm = next(e for e in history if e["action"] == "CONFIRM")
    assert confirm["actor"] == "user:dana"            # the confirmation is evented (doc 05 §3)
    assert svc.status(ADMIN, memory_id=confirm_id)["memory"]["strength"] == 6.0  # 1.0 + 5

    out = svc.review_staged(DANA, reject_id, action="reject", note="speculation")
    assert out["status"] == "archived"
    assert any(e["action"] == "REJECT" for e in svc.memory_history(ADMIN, reject_id))


def test_review_is_gated_to_scope_members_and_auditors(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="Atlas demo day is the last Friday of the quarter",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    with pytest.raises(AccessDenied):                 # agents do not confirm (doc 05 §3)
        svc.review_staged(SAGE_FOR_DANA, mid, action="confirm")
    with pytest.raises(AccessDenied):                 # eve is not a channel member
        svc.review_staged(Principal("user:eve"), mid, action="confirm")
    with pytest.raises(AccessDenied):                 # org-wide queue needs the auditor relation
        svc.staged_queue(DANA)
    assert mid in {m["id"] for m in svc.staged_queue(ADMIN)}


def test_sweep_archives_idle_staged_memories(svc):
    """Staged, never retrieved, no reinforcement → archive after 30 days."""
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="The old CI runner might still be in the rack",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    with svc.pool.connection() as conn:
        conn.execute("UPDATE memories SET recorded_at = now() - interval '31 days'"
                     " WHERE id=%s", (mid,))
    swept = Consolidator(svc).run()["swept"]
    assert mid in swept["staged_idle"]
    mem = svc.status(ADMIN, memory_id=mid)["memory"]
    assert mem["status"] == "archived"
    archive = next(e for e in svc.memory_history(ADMIN, mid) if e["action"] == "ARCHIVE")
    assert archive["actor"] == "system:consolidator"


def test_sweep_decay_flags_then_archives(svc):
    """Active, R below 0.05 → a PROPOSE-style event first (visibility),
    archive on a later run (doc 03 §5)."""
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="The 2025 oncall handbook is in the wiki archive",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    with svc.pool.connection() as conn:
        conn.execute("UPDATE memories SET last_accessed_at = now() - interval '365 days'"
                     " WHERE id=%s", (mid,))
    consolidator = Consolidator(svc)
    first = consolidator.run()["swept"]
    assert mid in first["decay_flagged"]
    assert svc.status(ADMIN, memory_id=mid)["memory"]["status"] == "active"  # not yet
    proposal = next(e for e in svc.memory_history(ADMIN, mid)
                    if e["action"] == "PROPOSE" and e["details"].get("proposal") == "archive")
    assert proposal["details"]["retention"] < 0.05

    later = datetime.now(timezone.utc) + timedelta(days=8)   # past the grace window
    second = consolidator.run(now=later)["swept"]
    assert mid in second["decay_archived"]
    assert svc.status(ADMIN, memory_id=mid)["memory"]["status"] == "archived"


def test_sweep_archives_expired_ttl(svc):
    mid = svc.remember(
        SAGE_FOR_DANA, LIFE_FLOW, content="The migration freeze lasts until end of June",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    with svc.pool.connection() as conn:
        conn.execute("UPDATE memories SET expires_at = now() - interval '1 day'"
                     " WHERE id=%s", (mid,))
    swept = Consolidator(svc).run()["swept"]
    assert mid in swept["ttl"]
    assert svc.status(ADMIN, memory_id=mid)["memory"]["status"] == "archived"
