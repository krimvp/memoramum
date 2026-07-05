"""The doc 07 §4 "metrics that matter", computed from the store
(observability.py, ADR-0008) and served at GET /v1/metrics.

The svc fixture is session-scoped, so absolute counts depend on which
suites ran before this one; these tests drive their own activity and
assert on structure, gates, and the deltas that activity must produce.
"""

import pytest
from conftest import ADMIN, DEPLOYS_FLOW, SAGE_FOR_DANA, DANA
from fastapi.testclient import TestClient

from memoramum.rest import create_app
from memoramum.service import AccessDenied


def test_metrics_require_org_admin_or_auditor(svc):
    with pytest.raises(AccessDenied):
        svc.metrics(SAGE_FOR_DANA)
    with pytest.raises(AccessDenied):
        svc.metrics(DANA)
    assert svc.metrics(ADMIN)["window_days"] == 30


def test_snapshot_covers_the_doc_07_4_families(svc):
    snap = svc.metrics(ADMIN, days=90)
    assert snap["window_days"] == 90

    rq = snap["retrieval_quality"]
    assert {"memories_read", "useful_read_ratio", "staged_precision",
            "contradiction_queue"} <= rq.keys()
    assert {"open", "escalated", "oldest_age_seconds"} == rq["contradiction_queue"].keys()

    assert {"decisions_by_verdict", "asks"} == snap["policy"].keys()
    for verdicts in snap["policy"]["decisions_by_verdict"].values():
        assert set(verdicts) <= {"allow", "stage", "ask", "deny"}

    lc = snap["lifecycle"]
    assert set(lc["tier_population"]) <= {"org", "surface", "container", "subject", "agent"}
    for statuses in lc["tier_population"].values():
        assert set(statuses) <= {"staged", "active", "invariant",
                                 "deprecated", "archived", "tombstoned"}

    assert snap["audit"]["event_log"]["lag_seconds"] == 0.0   # transactional (ADR-0004)
    assert snap["audit"]["erasure"]["slo_seconds"] == 72 * 3600
    assert {"consolidator_events_by_scope", "embeddings_written", "reads"} == snap["cost"].keys()

    with pytest.raises(ValueError):
        svc.metrics(ADMIN, days=0)


def test_reads_reinforcement_and_staged_triage_move_the_metrics(svc):
    before = svc.metrics(ADMIN)

    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy dashboards live in the #deploys channel topic.",
        kind="semantic", origin_kind="llm_inferred", categories=["process"],
        justification="observed while answering a dashboard question",
    )
    assert verdict["status"] == "staged"
    mid = verdict["memory_id"]

    hits = svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="where are the deploy dashboards")
    assert mid in {h["id"] for h in hits}                     # a READ delivered it
    svc.reinforce(SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=mid, signal="useful")
    svc.review_staged(DANA, mid, action="confirm")            # staged → active

    after = svc.metrics(ADMIN)
    rq_before, rq_after = before["retrieval_quality"], after["retrieval_quality"]
    assert rq_after["memories_read"] > rq_before["memories_read"]
    assert (rq_after["memories_reinforced_after_read"]
            > rq_before["memories_reinforced_after_read"])
    assert 0 < rq_after["useful_read_ratio"] <= 1
    assert rq_after["staged_promoted"] > rq_before["staged_promoted"]
    assert 0 < rq_after["staged_precision"] <= 1
    assert after["lifecycle"]["median_staged_to_active_seconds"] is not None

    # ADR-0006 batching is visible: never more READ events than memories.
    reads = after["cost"]["reads"]
    assert reads["memories_delivered"] >= reads["read_events"] > 0
    assert after["policy"]["decisions_by_verdict"]["agent:sage"]["stage"] > 0


def test_erasure_metrics_verify_attestations_against_the_key(svc):
    svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="A contractor prefers async reviews.",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["user:contractor-metrics"],
    )
    svc.request_erasure(ADMIN, subject="user:contractor-metrics")

    erasure = svc.metrics(ADMIN)["audit"]["erasure"]
    assert erasure["completed"] > 0
    assert erasure["over_slo"] == 0                           # in-transaction: well under 72 h
    assert erasure["max_completion_seconds"] < 72 * 3600
    assert erasure["attestation_coverage"] == 1.0             # every signature verifies


def test_rest_metrics_endpoint(svc):
    client = TestClient(create_app(service=svc))
    denied = client.get("/v1/metrics", headers={"X-Memoramum-Actor": "agent:sage",
                                                "X-Memoramum-On-Behalf-Of": "user:dana"})
    assert denied.status_code == 403

    ok = client.get("/v1/metrics?days=7", headers={"X-Memoramum-Actor": "user:admin"})
    assert ok.status_code == 200, ok.text
    snap = ok.json()
    assert snap["window_days"] == 7
    assert {"retrieval_quality", "policy", "lifecycle", "audit", "cost"} <= snap.keys()

    assert client.get("/v1/metrics?days=0",
                      headers={"X-Memoramum-Actor": "user:admin"}).status_code == 400
