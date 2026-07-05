"""The P4 exit criterion (doc 07 §6): the full worked scenario of
README.md, end-to-end across Slack + GitLab — learn (background
extraction), reinforce (consolidator promotion), promote (confirmed
crossing), cross-surface recall, forget (contradiction supersession),
audit (one traversal back to the source message).

Runs against a service wired with the deterministic dev stand-ins for the
LLM-shaped seams (marker extractor, overlap judge, hash embedder), in a
scenario-private slice of the scope tree so the shared fixtures stay
untouched."""

from datetime import datetime, timedelta, timezone

from conftest import ADMIN, SYSTEM, TEST_DB

import pytest

from memoramum.config import Settings
from memoramum.extraction import ExtractionWorker
from memoramum.principals import Flow, Principal
from memoramum.service import MemoryService

DANA = Principal("user:dana")
SAGE_FOR_DANA = Principal("agent:sage", "user:dana")
MARGE_FOR_AUTHOR = Principal("agent:marge", "user:mr-author")

CHANNEL, WORKSPACE = "channel/C9SCN", "workspace/T9SCN"
PROJECT, MR = "project/scn-api", "mr/9482"
CHANNEL_FLOW = Flow(surface="slack", container=CHANNEL,
                    participants=("user:dana", "user:li"), session_id="scn-slack")
MR_FLOW = Flow(surface="gitlab", container=MR,
               participants=("user:mr-author",), session_id="scn-mr")

RULE = "Team Atlas ships production changes only on Tuesdays"


@pytest.fixture(scope="module")
def scn(pool, svc):
    s = svc.create_scope
    s(SYSTEM, scope_id=WORKSPACE, family="container", parent_scope_id="surface:slack",
      surface="slack", external_ref={"team": "T9SCN"})
    s(SYSTEM, scope_id=CHANNEL, family="container", parent_scope_id=WORKSPACE,
      surface="slack", external_ref={"channel": "C9SCN", "name": "#deploys-scn"})
    s(SYSTEM, scope_id=PROJECT, family="container", parent_scope_id="surface:gitlab",
      surface="gitlab")
    s(SYSTEM, scope_id=MR, family="container", parent_scope_id=PROJECT, surface="gitlab")
    r = svc.set_relation
    for user in ("user:dana", "user:li", "user:mr-author"):
        r(SYSTEM, WORKSPACE, "member", user)
    for user in ("user:dana", "user:li"):
        r(SYSTEM, CHANNEL, "member", user)
    for scope, relation in ((CHANNEL, "reader_agent"), (CHANNEL, "writer_agent"),
                            (WORKSPACE, "reader_agent"), (WORKSPACE, "writer_agent")):
        r(SYSTEM, scope, relation, "agent:sage")
    # README step 4: Marge is enrolled in the shared scope.
    r(SYSTEM, WORKSPACE, "reader_agent", "agent:marge")

    return MemoryService(pool, Settings(database_url=TEST_DB, embedder="hash",
                                        judge="overlap", extractor="marker"))


def _slack_message(scn, content, *, author, when):
    return scn.register_episode(
        SYSTEM, scope_id=CHANNEL, source_kind="slack_message",
        external_ref={"channel": "C9SCN", "ts": str(when.timestamp())},
        content=content, author=author, occurred_at=when,
    )


def test_the_full_worked_scenario(scn):
    now = datetime.now(timezone.utc)

    # -- Step 1: Learn. dana states the rule; background extraction
    # proposes a staged memory scoped to the channel, provenance pointing
    # at her exact message.
    msg = _slack_message(scn, f"reminder: {RULE}", author="user:dana",
                         when=now - timedelta(hours=1))
    summary = ExtractionWorker(scn).run(now, only_scopes=[CHANNEL])
    proposed = summary[CHANNEL]["proposed"]
    assert [p["decision"] for p in proposed] == ["stage"]
    mid = proposed[0]["memory_id"]
    status = scn.status(ADMIN, memory_id=mid)
    assert status["memory"]["status"] == "staged"
    assert status["memory"]["scope_id"] == CHANNEL
    assert status["provenance"]["origin_kind"] == "agent_observed"
    assert ("episode", str(msg["id"])) in {
        (s["source_type"], s["source_id"]) for s in status["sources"]}

    # -- Step 2: Reinforce. Two weeks later the fact is re-observed from
    # an independent episode; the consolidator (micro-run) promotes it.
    _slack_message(scn, f"note: {RULE}", author="user:li",
                   when=now + timedelta(days=14))
    ExtractionWorker(scn).run(now + timedelta(days=14, hours=1), only_scopes=[CHANNEL])
    status = scn.status(ADMIN, memory_id=mid)
    assert status["memory"]["status"] == "active"
    history = scn.memory_history(ADMIN, mid)
    reinforce = next(e for e in history if e["action"] == "REINFORCE")
    assert reinforce["details"]["signal"] == "re_observation"
    promote = next(e for e in history if e["action"] == "PROMOTE_STATUS")
    assert promote["actor"] == "system:consolidator"

    # -- Step 3: Promote. "make sure the other teams know this too" —
    # a shared-scope crossing asks; dana confirms; the move is evented.
    out = scn.promote(SAGE_FOR_DANA, CHANNEL_FLOW, memory_id=mid,
                      target_scope=WORKSPACE,
                      justification="dana asked to share the rule with all teams")
    assert out["decision"] == "ask"
    scn.confirm_pending(DANA, out["pending_id"], approved=True)
    assert scn.get_memory(ADMIN, mid)["scope_id"] == WORKSPACE

    # -- Step 4: Cross-surface recall. Marge, reviewing a Friday MR on
    # GitLab, resolves her chain (mr → project → org + the shared scope
    # she is enrolled in) and retrieves the rule.
    hits = scn.recall(MARGE_FOR_AUTHOR, MR_FLOW,
                      query="when does team atlas ship production changes")
    assert mid in [h["id"] for h in hits]
    hit = next(h for h in hits if h["id"] == mid)
    assert "user:dana" in hit["provenance"]     # citable back to the source
    scn.reinforce(MARGE_FOR_AUTHOR, MR_FLOW, memory_id=mid, signal="useful",
                  note="flagged a Friday deploy in MR !9482")

    # -- Step 5: Forget. "we've moved to daily deploys": the contradicting
    # observation stays staged (staged input cannot assassinate an active
    # memory), is held for review, and the human resolution supersedes —
    # validity window closed, successor linked, history retained.
    changed = now + timedelta(days=90)
    _slack_message(scn, "psa: Team Atlas now ships production changes daily",
                   author="user:dana", when=changed)
    summary = ExtractionWorker(scn).run(changed + timedelta(hours=1), only_scopes=[CHANNEL])
    challenger = summary[CHANNEL]["proposed"][0]["memory_id"]
    assert scn.status(ADMIN, memory_id=challenger)["memory"]["status"] == "staged"
    propose = next(e for e in scn.memory_history(ADMIN, challenger)
                   if e["action"] == "PROPOSE")
    assert propose["details"]["contradicts"] == mid
    queue = [q for q in scn.contradictions(ADMIN) if q["challenger_id"] == challenger]
    assert queue and str(queue[0]["contradicted_id"]) == mid
    scn.resolve_contradiction(DANA, queue[0]["id"], resolution="supersede",
                              note="daily deploys confirmed in the channel")
    old = scn.get_memory(ADMIN, mid)
    assert old["status"] == "deprecated"
    assert old["superseded_by"] == challenger
    assert old["invalid_at"] is not None        # the validity window closed
    assert mid not in [h["id"] for h in scn.recall(
        MARGE_FOR_AUTHOR, MR_FLOW, query="team atlas production changes")]
    # …but the successor lives on where it was learned.
    assert challenger in [h["id"] for h in scn.recall(
        SAGE_FOR_DANA, CHANNEL_FLOW, query="team atlas production changes")]

    # -- Step 6: Audit. "why did Marge warn about Tuesday deploys on that
    # MR?" — one traversal: READ event → memory → its promotion and
    # reinforcements → dana's original message.
    reads = scn.audit_events(ADMIN, action="READ", actor="agent:marge", scope_id=MR)
    assert any(mid in e["details"]["memory_ids"] for e in reads)
    actions = [e["action"] for e in scn.memory_history(ADMIN, mid)]
    for expected in ("PROPOSE", "REINFORCE", "PROMOTE_STATUS", "CONFIRM",
                     "PROMOTE_SCOPE", "SUPERSEDE"):
        assert expected in actions, expected
    sources = {s["source_id"] for s in scn.status(ADMIN, memory_id=mid)["sources"]
               if s["source_type"] == "episode"}
    assert str(msg["id"]) in sources
    episode = scn.get_episode(ADMIN, str(msg["id"]))
    assert episode["author"] == "user:dana"
    assert episode["external_ref"]["channel"] == "C9SCN"    # the deep link
