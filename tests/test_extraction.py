"""Extraction workers (doc 07 §1): debounced background reflection over
recent episodes, proposing `agent_observed` candidates through the same
write pipeline as any agent."""

from datetime import datetime, timedelta, timezone

from conftest import SYSTEM

import pytest

from memoramum.extraction import ExtractionWorker

SCOPE = "channel/C0EXT"


@pytest.fixture(scope="module")
def world(svc):
    svc.create_scope(SYSTEM, scope_id=SCOPE, family="container",
                     parent_scope_id="workspace/T024B", surface="slack",
                     external_ref={"name": "#extraction"})
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, SCOPE, "member", user)
    svc.set_relation(SYSTEM, SCOPE, "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, SCOPE, "writer_agent", "agent:sage")
    return svc


def _episode(svc, content, *, author="user:dana", ago_minutes=45):
    return svc.register_episode(
        SYSTEM, scope_id=SCOPE, source_kind="slack_message",
        external_ref={"channel": "C0EXT", "ts": str(ago_minutes)},
        content=content, author=author,
        occurred_at=datetime.now(timezone.utc) - timedelta(minutes=ago_minutes),
    )


def test_extraction_proposes_through_the_write_pipeline(world):
    svc = world
    ep = _episode(svc, "reminder: staging refreshes wipe local feature branches")
    _episode(svc, "anyone up for lunch?")   # no standing statement — no candidate

    worker = ExtractionWorker(svc)
    now = datetime.now(timezone.utc)
    assert SCOPE in {r["scope_id"] for r in worker.due_scopes(now)}  # lull passed

    summary = worker.run(now, only_scopes=[SCOPE])
    proposed = summary[SCOPE]["proposed"]
    assert [p["decision"] for p in proposed] == ["stage"]   # staged by default (ADR-0003)
    mid = proposed[0]["memory_id"]

    status = svc.status(SYSTEM, memory_id=mid)
    assert status["memory"]["status"] == "staged"
    assert status["memory"]["content"] == "staging refreshes wipe local feature branches"
    assert status["provenance"]["origin_kind"] == "agent_observed"
    # The worker acted as the enrolled agent, and policy decided (doc 07 §1).
    assert status["provenance"]["responsible_agent"] == "agent:sage"
    assert {(s["source_type"], s["source_id"]) for s in status["sources"]} == {
        ("episode", str(ep["id"]))
    }
    # Scope-member author → the doc 06 §3 trust derivation, above the floor.
    assert status["memory"]["trust_score"] == pytest.approx(0.6)


def test_debounce_and_cap(world):
    svc = world
    worker = ExtractionWorker(svc)
    now = datetime.now(timezone.utc)
    _episode(svc, "note: the retro doc lives in the canvas", ago_minutes=0)

    # Fresh activity: not at conversation-lull yet.
    assert SCOPE not in {r["scope_id"] for r in worker.due_scopes(now)}
    # …but the cap bounds the wait (doc 07 §1: debounce 30 min, cap 4 h).
    assert SCOPE in {r["scope_id"] for r in worker.due_scopes(now + timedelta(hours=5))}

    summary = worker.run(now + timedelta(hours=5), only_scopes=[SCOPE])
    assert len(summary[SCOPE]["proposed"]) == 1

    # The watermark advanced: a re-run proposes nothing (idempotent).
    assert SCOPE not in {r["scope_id"] for r in worker.due_scopes(now + timedelta(hours=5))}


def test_no_enrolled_writer_means_nothing_is_learned(svc):
    scope = "channel/C0NOAGENT"
    svc.create_scope(SYSTEM, scope_id=scope, family="container",
                     parent_scope_id=None, surface="slack")
    svc.register_episode(
        SYSTEM, scope_id=scope, source_kind="slack_message",
        external_ref={"channel": "C0NOAGENT", "ts": "1"},
        content="reminder: this scope has no agent", author="user:dana",
        occurred_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    worker = ExtractionWorker(svc)
    summary = worker.run(datetime.now(timezone.utc), only_scopes=[scope])
    assert summary[scope]["proposed"] == []
    assert "no writer_agent" in summary[scope]["skipped"]   # default deny (doc 05 §1)
