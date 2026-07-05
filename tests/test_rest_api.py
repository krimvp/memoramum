"""The REST facade (doc 04 §5) over the same API core."""

import pytest
from fastapi.testclient import TestClient

from memoramum.rest import create_app


@pytest.fixture(scope="module")
def client(svc):
    return TestClient(create_app(service=svc))


AS_SAGE_FOR_DANA = {"X-Memoramum-Actor": "agent:sage", "X-Memoramum-On-Behalf-Of": "user:dana"}
AS_ADMIN = {"X-Memoramum-Actor": "user:admin"}
AS_SYSTEM = {"X-Memoramum-Actor": "system:ingest"}


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True, "phase": "P4"}


def test_episode_and_context_block_roundtrip(client):
    ep = client.post("/v1/episodes", headers=AS_SYSTEM, json={
        "scope_id": "channel/C0DEP", "source_kind": "slack_message",
        "external_ref": {"channel": "C0DEP", "ts": "1709481700.456"},
        "content": "deploy freezes are announced a week ahead",
        "author": "user:dana",
    })
    assert ep.status_code == 200, ep.text

    block = client.post("/v1/context-block", json={
        "principal": {"agent": "agent:sage", "on_behalf_of": "user:dana"},
        "flow": {"surface": "slack", "container": "channel/C0DEP",
                 "participants": ["user:dana", "user:li"]},
        "focus": "deploy schedule",
        "token_budget": 1200,
    })
    assert block.status_code == 200, block.text
    assert block.json()["block"].startswith("<memoramum ")


def test_memory_views_and_history(client, svc):
    from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Rollbacks are coordinated in the deploys channel",
        kind="semantic", origin_kind="explicit_user_ask", subjects=["user:dana"],
    )["memory_id"]

    assert client.get(f"/v1/memories/{mid}", headers=AS_SAGE_FOR_DANA).json()["id"] == mid
    history = client.get(f"/v1/memories/{mid}/history", headers=AS_ADMIN).json()
    assert "PROPOSE" in {e["action"] for e in history}

    subj = client.get("/v1/subjects/user:dana/memories",
                      headers={"X-Memoramum-Actor": "user:dana"}).json()
    assert mid in {m["id"] for m in subj["memories"]}

    inventory = client.get("/v1/scopes/channel/C0DEP/memories", headers=AS_SAGE_FOR_DANA).json()
    assert mid in {m["id"] for m in inventory["memories"]}

    # The intersection rule at the REST layer: eve is a workspace member but
    # not a channel member, so even an enrolled agent reading for her is
    # denied (doc 05 §4.1).
    denied = client.get(f"/v1/memories/{mid}",
                        headers={"X-Memoramum-Actor": "agent:sage",
                                 "X-Memoramum-On-Behalf-Of": "user:eve"})
    assert denied.status_code == 403


def test_audit_endpoint_gated(client):
    assert client.get("/v1/audit/events", headers=AS_ADMIN).status_code == 200
    assert client.get("/v1/audit/events", headers=AS_SAGE_FOR_DANA).status_code == 403


def test_review_surface_roundtrip(client, svc):
    from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy retro actions are tracked in the channel topic",
        kind="semantic", origin_kind="llm_inferred",
    )["memory_id"]
    as_dana = {"X-Memoramum-Actor": "user:dana"}

    queue = client.get("/v1/review/staged", params={"scope_id": "channel/C0DEP"},
                       headers=as_dana).json()
    assert mid in {m["id"] for m in queue}

    out = client.post(f"/v1/memories/{mid}/review", headers=as_dana,
                      json={"action": "confirm", "note": "dana vouches"})
    assert out.status_code == 200 and out.json()["status"] == "active"

    assert client.get("/v1/review/contradictions", headers=as_dana).status_code == 200
    # Agents do not confirm (doc 05 §3).
    denied = client.post(f"/v1/memories/{mid}/review", headers=AS_SAGE_FOR_DANA,
                         json={"action": "confirm"})
    assert denied.status_code == 403


def test_policy_and_pending_surface(client, svc):
    """The P3 admin surface: policy documents (YAML in), simulation, and
    the ask-confirmation queue (doc 05 §5, doc 04 §5)."""
    from conftest import DEPLOYS_FLOW, SYSTEM
    from memoramum.principals import Principal
    as_root = {"X-Memoramum-Actor": "user:root"}
    as_dana = {"X-Memoramum-Actor": "user:dana"}

    yaml_doc = """
policy: resty-default
layer: agent
applies_to: agent:resty
learning:
  strategies:
    - { name: careful, origin_kinds: [explicit_user_ask], decision: ask }
"""
    put = client.post("/v1/policies", headers=as_root, json={"document": yaml_doc})
    assert put.status_code == 200 and put.json()["version"] >= 1
    assert client.post("/v1/policies", headers=as_dana,
                       json={"document": yaml_doc}).status_code == 403
    listed = client.get("/v1/policies", headers=as_root).json()
    assert "resty-default" in {p["name"] for p in listed}

    sim = client.post("/v1/policies/simulate", headers=AS_ADMIN,
                      json={"document": yaml_doc, "days": 30})
    assert sim.status_code == 200 and "summary" in sim.json()

    svc.set_relation(SYSTEM, "channel/C0DEP", "reader_agent", "agent:resty")
    svc.set_relation(SYSTEM, "channel/C0DEP", "writer_agent", "agent:resty")
    ask = svc.remember(
        Principal("agent:resty", "user:dana"), DEPLOYS_FLOW,
        content="Release notes get a summary thread in the channel",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    assert ask["decision"] == "ask"
    queue = client.get("/v1/review/pending", headers=as_dana).json()
    assert ask["pending_id"] in {p["id"] for p in queue}
    out = client.post(f"/v1/review/pending/{ask['pending_id']}", headers=as_dana,
                      json={"approved": True})
    assert out.status_code == 200 and out.json()["status"] == "active"


def test_incident_and_erasure_endpoints_are_gated(client):
    """The P4 surface exists and is privileged (doc 06 §2–3): incident
    levers and erasure are not agent powers."""
    denied = client.post("/v1/quarantine", headers=AS_SAGE_FOR_DANA,
                         json={"author": "user:eve"})
    assert denied.status_code == 403
    assert client.get("/v1/quarantine", headers=AS_SAGE_FOR_DANA).status_code == 403
    denied = client.post("/v1/erasure-requests", headers=AS_SAGE_FOR_DANA,
                         json={"subject": "user:li"})
    assert denied.status_code == 403
    denied = client.post("/v1/agents/agent:sage/freeze", headers=AS_SAGE_FOR_DANA,
                         json={"frozen": True})
    assert denied.status_code == 403
    # An empty quarantine predicate is refused, even for an admin.
    assert client.post("/v1/quarantine", headers=AS_ADMIN, json={}).status_code == 400
