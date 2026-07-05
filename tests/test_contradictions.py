"""P2 exit criterion 2 (doc 07 §6): the contradiction demo — worked-
scenario step 5 — plus the held-contradiction rule (staged input can never
supersede active memories) and its review resolutions.

Contradiction judging is LLM-shaped; these tests run the deterministic
'overlap' dev judge behind the same seam a model judge plugs into."""

from datetime import datetime, timedelta, timezone

import pytest
from conftest import ADMIN, DANA, SAGE_FOR_DANA, SYSTEM, TEST_DB

from memoramum.config import Settings
from memoramum.consolidator import Consolidator
from memoramum.principals import Flow
from memoramum.service import MemoryService

SIM_FLOW = Flow(surface="slack", container="channel/C0SIM",
                participants=("user:dana", "user:li"), session_id="sess-sim")


@pytest.fixture(scope="module")
def sim(svc):
    """The worked-scenario deploy story replayed in a dedicated channel,
    against a service wired with the 'overlap' dev judge."""
    svc.create_scope(SYSTEM, scope_id="channel/C0SIM", family="container",
                     parent_scope_id="workspace/T024B", surface="slack",
                     external_ref={"name": "#deploys-sim"})
    for user in ("user:dana", "user:li", "user:admin"):
        svc.set_relation(SYSTEM, "channel/C0SIM", "member", user)
    svc.set_relation(SYSTEM, "channel/C0SIM", "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, "channel/C0SIM", "writer_agent", "agent:sage")
    return MemoryService(svc.pool, Settings(database_url=TEST_DB, embedder="hash",
                                            judge="overlap"))


def _episode(sim, author, content, occurred_at=None):
    return sim.register_episode(
        SYSTEM, scope_id="channel/C0SIM", source_kind="slack_message",
        external_ref={"channel": "C0SIM"}, content=content, author=author,
        occurred_at=occurred_at,
    )["id"]


def test_scenario_step5_supersession(sim):
    """Months later someone posts "we've moved to daily deploys":
    contradiction detection closes the old validity window, creates the
    successor, links the two; the old memory stops surfacing but stays
    queryable for history."""
    ep_old = _episode(sim, "user:dana",
                      "reminder: Team Atlas deploys to production only on Tuesdays",
                      occurred_at=datetime(2026, 3, 3, tzinfo=timezone.utc))
    old = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Team Atlas deploys to production only on Tuesdays",
        kind="semantic", origin_kind="explicit_user_ask", subjects=["team:atlas"],
        source_episode_ids=[ep_old], justification="dana asked to keep the deploy rule",
    )
    assert old["status"] == "active"

    changed = datetime(2026, 6, 10, tzinfo=timezone.utc)
    ep_new = _episode(sim, "user:li", "heads up: team atlas has moved to daily deploys now",
                      occurred_at=changed)
    new = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Team Atlas deploys to production daily",
        kind="semantic", origin_kind="explicit_user_ask", subjects=["team:atlas"],
        source_episode_ids=[ep_new], justification="li asked to update the deploy rule",
    )
    assert new["supersedes"] == old["memory_id"]

    # (2) the old memory's validity window is closed with the new
    # episode's occurred_at; (3) deprecated + superseded_by; (4) SUPERSEDE.
    deprecated = sim.get_memory(ADMIN, old["memory_id"])
    assert deprecated["status"] == "deprecated"
    assert deprecated["invalid_at"] == changed.isoformat()
    assert deprecated["superseded_by"] == new["memory_id"]
    supersede = next(e for e in sim.memory_history(ADMIN, old["memory_id"])
                     if e["action"] == "SUPERSEDE")
    assert supersede["details"]["superseded_by"] == new["memory_id"]

    # (1) the successor derives from both the new episode and the
    # contradicted memory.
    sources = sim.status(ADMIN, memory_id=new["memory_id"])["sources"]
    assert {(s["source_type"], s["source_id"]) for s in sources} == {
        ("episode", ep_new), ("memory", old["memory_id"])}

    # The old memory stops surfacing in default recall…
    current = sim.recall(SAGE_FOR_DANA, SIM_FLOW, query="atlas production deploys")
    ids = [h["id"] for h in current]
    assert new["memory_id"] in ids and old["memory_id"] not in ids

    # …but remains queryable for history: an as-of query (audit-gated)
    # still returns the Tuesday belief for April. (The row was inserted at
    # test runtime; put recorded_at on the scenario date first.)
    with sim.pool.connection() as conn:
        conn.execute("UPDATE memories SET recorded_at=%s WHERE id=%s",
                     (datetime(2026, 3, 3, tzinfo=timezone.utc), old["memory_id"]))
    believed = sim.recall(ADMIN, SIM_FLOW, query="atlas production deploys",
                          as_of=datetime(2026, 4, 1, tzinfo=timezone.utc))
    assert [h["id"] for h in believed] == [old["memory_id"]]


def test_staged_input_cannot_supersede_active_memories(sim):
    """The poisoning defense (doc 03 §3, doc 06 §3): a staged challenger is
    held for review, and cannot be promoted past the held contradiction."""
    active = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Deploy freezes start every Friday afternoon",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    ep = _episode(sim, "user:li", "actually deploy freezes start monday mornings")
    challenger = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Deploy freezes start Monday mornings",
        kind="semantic", origin_kind="llm_inferred", source_episode_ids=[ep],
    )
    assert challenger["status"] == "staged"
    assert challenger["contradicts"] == active["memory_id"]
    assert "held" in challenger["reason"]

    untouched = sim.get_memory(ADMIN, active["memory_id"])
    assert untouched["status"] == "active"            # nothing assassinated
    assert untouched["invalid_at"] is None

    queue = [q for q in sim.contradictions(DANA)
             if q["challenger_id"] == challenger["memory_id"]]
    assert queue and queue[0]["contradicted_id"] == active["memory_id"]

    # Reinforcement can't promote around the hold, and neither can a
    # direct confirm — the resolution path is the only way through.
    sim.reinforce(SAGE_FOR_DANA, SIM_FLOW, memory_id=challenger["memory_id"], signal="useful")
    sim.reinforce(SAGE_FOR_DANA, SIM_FLOW, memory_id=challenger["memory_id"], signal="useful")
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "staged"
    with pytest.raises(ValueError, match="held contradiction"):
        sim.review_staged(DANA, challenger["memory_id"], action="confirm")

    # dana (scope member) resolves: the challenger wins.
    out = sim.resolve_contradiction(DANA, queue[0]["id"], resolution="supersede",
                                    note="li is right, the freeze moved")
    assert out["resolution"] == "supersede"
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "active"
    old = sim.get_memory(ADMIN, active["memory_id"])
    assert old["status"] == "deprecated"
    assert old["superseded_by"] == challenger["memory_id"]
    confirm = next(e for e in sim.memory_history(ADMIN, challenger["memory_id"])
                   if e["action"] == "CONFIRM")
    assert confirm["actor"] == "user:dana"


def test_keep_both_closes_the_validity_window_only(sim):
    """Resolution 'keep both with validity windows' (doc 07 §2): the old
    fact stays active but describes a fact that ended — it leaves current
    recall without being superseded."""
    old = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Release notes are posted to the changelog page",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    ep = _episode(sim, "user:li", "release notes are posted in the announcements channel now")
    challenger = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Release notes are posted in the announcements channel",
        kind="semantic", origin_kind="llm_inferred", source_episode_ids=[ep],
    )
    queue_id = next(q["id"] for q in sim.contradictions(ADMIN)
                    if q["challenger_id"] == challenger["memory_id"])
    ended = datetime(2026, 7, 1, tzinfo=timezone.utc)
    sim.resolve_contradiction(ADMIN, queue_id, resolution="keep_both", invalid_at=ended)

    old_mem = sim.get_memory(ADMIN, old["memory_id"])
    assert old_mem["status"] == "active"              # not deprecated: both stand
    assert old_mem["invalid_at"] == ended.isoformat()
    assert old_mem["superseded_by"] is None
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "active"
    ids = [h["id"] for h in sim.recall(SAGE_FOR_DANA, SIM_FLOW, query="release notes posted")]
    assert challenger["memory_id"] in ids and old["memory_id"] not in ids


def test_reject_resolution_archives_the_challenger(sim):
    old = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Incident bridges are opened in the war room channel",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    challenger = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Incident bridges are opened in a video call",
        kind="semantic", origin_kind="llm_inferred",
    )
    queue_id = next(q["id"] for q in sim.contradictions(ADMIN)
                    if q["challenger_id"] == challenger["memory_id"])
    sim.resolve_contradiction(DANA, queue_id, resolution="reject", note="misheard")
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "archived"
    assert sim.get_memory(ADMIN, old["memory_id"])["status"] == "active"


def test_consolidator_escalates_and_archives_stale_holds(sim, svc):
    """doc 03 §5: unresolved held contradictions escalate to review after
    7 days and the challenger archives after 30."""
    old = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Load tests run against the perf cluster",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    challenger = sim.remember(
        SAGE_FOR_DANA, SIM_FLOW,
        content="Load tests run against a scratch cluster",
        kind="semantic", origin_kind="llm_inferred",
    )
    queue_id = next(q["id"] for q in sim.contradictions(ADMIN)
                    if q["challenger_id"] == challenger["memory_id"])
    with svc.pool.connection() as conn:
        conn.execute("UPDATE contradiction_queue SET queued_at = now() - interval '8 days'"
                     " WHERE id=%s", (queue_id,))
    out = Consolidator(sim).run()["contradictions"]
    assert queue_id in out["escalated"]
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "staged"  # still up

    with svc.pool.connection() as conn:
        conn.execute("UPDATE contradiction_queue SET queued_at = now() - interval '31 days'"
                     " WHERE id=%s", (queue_id,))
    out = Consolidator(sim).run()["contradictions"]
    assert challenger["memory_id"] in out["archived"]
    assert sim.get_memory(ADMIN, challenger["memory_id"])["status"] == "archived"
    resolved = next(q for q in sim.contradictions(ADMIN, include_resolved=True)
                    if q["id"] == queue_id)
    assert resolved["resolution"] == "archived"
    assert sim.get_memory(ADMIN, old["memory_id"])["status"] == "active"


def test_history_retention_sweep_archives_old_deprecated(sim, svc):
    """Scenario step 5's tail (doc 03 §7): a year after supersession the
    deprecated memory falls out of the online history window."""
    olds = [m for m in sim.scope_memories(ADMIN, "channel/C0SIM")["memories"]
            if m["status"] == "deprecated"]
    assert olds
    target = olds[0]["id"]
    with svc.pool.connection() as conn:
        conn.execute("UPDATE memory_events SET at = at - interval '400 days'"
                     " WHERE memory_id=%s AND action='SUPERSEDE'", (target,))
    swept = Consolidator(sim).run()["swept"]
    assert target in swept["history_retention"]
    assert sim.get_memory(ADMIN, target)["status"] == "archived"
