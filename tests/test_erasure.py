"""The GDPR erasure pipeline (doc 06 §2.2): enumerate the derived
closure, split consolidated derivatives, hard-delete content, tombstone,
verify, and sign an attestation — while the audit chain survives."""

from datetime import datetime, timezone

from conftest import ADMIN, SYSTEM

import pytest

from memoramum import erasure
from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

VICTOR = "user:victor"
SAGE_FOR_DANA = Principal("agent:sage", "user:dana")
CHANNEL = "channel/C0ERAS"
FLOW = Flow(surface="slack", container=CHANNEL,
            participants=("user:dana", VICTOR), session_id="s-eras")


@pytest.fixture(scope="module")
def world(svc):
    svc.create_scope(SYSTEM, scope_id=CHANNEL, family="container",
                     parent_scope_id="workspace/T024B", surface="slack")
    for scope in ("org:acme", "workspace/T024B"):
        svc.set_relation(SYSTEM, scope, "member", VICTOR)
    for user in ("user:dana", "user:li", VICTOR):
        svc.set_relation(SYSTEM, CHANNEL, "member", user)
    svc.set_relation(SYSTEM, CHANNEL, "reader_agent", "agent:sage")
    svc.set_relation(SYSTEM, CHANNEL, "writer_agent", "agent:sage")

    ep = svc.register_episode(
        SYSTEM, scope_id=CHANNEL, source_kind="slack_message",
        external_ref={"channel": "C0ERAS", "ts": "victor-1"},
        content="victor: my canary rota slot is Thursdays", author=VICTOR,
        occurred_at=datetime.now(timezone.utc),
    )
    about = svc.remember(
        SAGE_FOR_DANA, FLOW,
        content="victor covers the canary rota on Thursdays", kind="semantic",
        origin_kind="llm_inferred", subjects=[VICTOR], source_episode_ids=[ep["id"]],
    )["memory_id"]
    survivor = svc.remember(
        SAGE_FOR_DANA, FLOW,
        content="The canary rota is staffed every weekday", kind="semantic",
        origin_kind="explicit_user_ask",
    )["memory_id"]
    mixed = svc.remember(
        Principal("system:consolidator"), FLOW,
        content="Canary coverage spans the whole week including victor's slot",
        kind="semantic", origin_kind="consolidated",
        source_memory_ids=[about, survivor],
    )["memory_id"]
    return {"episode": ep, "about": about, "survivor": survivor, "mixed": mixed}


def test_erasure_pipeline_end_to_end(svc, pool, world):
    out = svc.request_erasure(ADMIN, subject=VICTOR, note="departed; DSAR ticket 88")
    att = out["attestation"]

    # 1. Enumeration caught the subject_ids hit AND the derived closure.
    assert {world["about"], world["mixed"]} <= set(att["memories_tombstoned"])
    assert world["survivor"] not in att["memories_tombstoned"]

    # 2. The mixed consolidated memory was re-generated from survivors.
    assert att["regenerated"] and att["regenerated"][0]["replaced"] == world["mixed"]
    successor = att["regenerated"][0]["successor"]
    status = svc.status(ADMIN, memory_id=successor)
    assert status["provenance"]["origin_kind"] == "consolidated"
    sources = {s["source_id"] for s in status["sources"] if s["source_type"] == "memory"}
    assert sources == {world["survivor"]}    # no edge back to erased inputs

    # 3–4. Content, embeddings and episode verbatims are physically gone;
    # tombstones and content-free events remain.
    with pool.connection() as conn:
        cur = conn.cursor()
        for mid in (world["about"], world["mixed"]):
            row = cur.execute(
                "SELECT status, content, content_embedding FROM memories WHERE id=%s", (mid,)
            ).fetchone()
            assert row["status"] == "tombstoned"
            assert row["content"] == "" and row["content_embedding"] is None
        ep = cur.execute(
            "SELECT content, external_ref FROM episodes WHERE id=%s",
            (world["episode"]["id"],),
        ).fetchone()
        assert ep["content"] is None and ep["external_ref"]   # ref stays, verbatim goes
    tombstone = next(e for e in svc.memory_history(ADMIN, world["about"])
                     if e["action"] == "TOMBSTONE")
    assert tombstone["details"]["legal_basis"] == "gdpr_art_17"
    assert "content" not in tombstone["details"]

    # 5. Verified and signed; the record is retrievable later.
    assert att["verified"] is True and all(v == 0 for v in att["checks"].values())
    assert erasure.verify_attestation(att, svc.settings.attestation_key)
    assert not erasure.verify_attestation(att, "wrong-key")
    stored = svc.erasure_request(ADMIN, out["request_id"])
    assert stored["completed_at"] and stored["attestation"]["signature"] == att["signature"]

    # DSAR view: nothing live remains about the subject.
    view = svc.status(ADMIN, subject=VICTOR)
    assert all(m["status"] == "tombstoned" for m in view["memories"])


def test_erasure_is_for_the_subject_or_privileged_roles(svc):
    with pytest.raises(AccessDenied):
        svc.request_erasure(SAGE_FOR_DANA, subject="user:li")
    # The subject themself may always ask.
    out = svc.request_erasure(Principal("user:wanda"), subject="user:wanda")
    assert out["attestation"]["verified"] is True
