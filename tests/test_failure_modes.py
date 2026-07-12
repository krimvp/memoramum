"""The doc 07 §5 failure modes, driven one row at a time: membership-sync
staleness (private surface scopes fail closed beyond the bound, others
serve with staleness noted in the READ event), policy engine unreachable
(writes fail closed, reads restricted to the agent's own scope), and the
MCP facade's explicit unavailability."""

import json

import psycopg
import pytest
from conftest import ADMIN, DEPLOYS_FLOW, SAGE_FOR_DANA, SYSTEM
from fastapi.testclient import TestClient

from memoramum.mcp_server import build_server
from memoramum.principals import Flow
from memoramum.rest import create_app
from memoramum.service import AccessDenied, PolicyUnavailable

DM_FLOW = Flow(surface="slack", container="dm/D888",
               participants=("user:dana",), session_id="sess-fm")


@pytest.fixture(scope="module")
def private_dm(svc):
    """A private DM scope Sage can normally read, for the fail-closed arc."""
    svc.create_scope(SYSTEM, scope_id="dm/D888", family="container",
                     parent_scope_id="workspace/T024B", surface="slack",
                     trust_class="private")
    for relation in ("reader_agent", "writer_agent"):
        svc.set_relation(SYSTEM, "dm/D888", relation, "agent:sage")
    svc.set_relation(SYSTEM, "dm/D888", "member", "user:dana")
    return "dm/D888"


def _backdate_sync(svc, surface: str, minutes: int) -> None:
    with svc.pool.connection() as conn:
        conn.execute(
            "UPDATE membership_sync SET synced_at = now() - make_interval(mins => %s)"
            " WHERE surface = %s",
            (minutes, surface),
        )


def test_stale_membership_fails_private_scopes_closed(svc, private_dm):
    svc.record_membership_sync(SYSTEM, surface="slack")
    dm_mid = svc.remember(
        SAGE_FOR_DANA, DM_FLOW,
        content="The pager escalation shortcut is /esc in this DM.",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    ws_mid = svc.remember(
        SAGE_FOR_DANA, Flow(surface="slack", container="workspace/T024B",
                            participants=("user:dana",), session_id="sess-fm"),
        content="Workspace-wide: incident retros happen on Thursdays.",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]

    # Fresh sync: the private DM serves normally.
    hits = svc.recall(SAGE_FOR_DANA, DM_FLOW, query="pager escalation shortcut")
    assert dm_mid in {h["id"] for h in hits}

    # Beyond the bound: the private scope fails closed…
    _backdate_sync(svc, "slack", minutes=10)
    hits = svc.recall(SAGE_FOR_DANA, DM_FLOW, query="pager escalation shortcut")
    assert dm_mid not in {h["id"] for h in hits}

    # …while non-private scopes serve, with the staleness noted in the event.
    hits = svc.recall(SAGE_FOR_DANA, DM_FLOW, query="incident retros Thursdays")
    assert ws_mid in {h["id"] for h in hits}
    read = next(e for e in svc.audit_events(ADMIN, action="READ", actor="agent:sage")
                if ws_mid in e["details"]["memory_ids"])
    assert read["details"]["membership_staleness_seconds"] >= 600

    # A fresh heartbeat restores the private scope: it was the watermark.
    svc.record_membership_sync(SYSTEM, surface="slack")
    hits = svc.recall(SAGE_FOR_DANA, DM_FLOW, query="pager escalation shortcut")
    assert dm_mid in {h["id"] for h in hits}


def test_never_synced_surfaces_carry_no_staleness(svc):
    # gitlab has no watermark: membership is authored in the service
    # directly, so nothing fails closed and no staleness is noted.
    mr_flow = Flow(surface="gitlab", container="mr/482",
                   participants=("user:dana",), session_id="sess-fm2")
    block = svc.context_block(SAGE_FOR_DANA, mr_flow)
    assert block["block"].startswith("<memoramum ")


def test_heartbeats_belong_to_platform_ingestion(svc):
    with pytest.raises(AccessDenied):
        svc.record_membership_sync(SAGE_FOR_DANA, surface="slack")


def test_policy_unreachable_fails_writes_closed(svc, monkeypatch):
    def down(*a, **kw):
        raise psycopg.OperationalError("policy tables unreachable")

    monkeypatch.setattr(svc, "_layers", down)
    with pytest.raises(PolicyUnavailable):
        svc.remember(
            SAGE_FOR_DANA, DEPLOYS_FLOW,
            content="This write must not land without a verdict.",
            kind="semantic", origin_kind="explicit_user_ask",
        )


def test_policy_unreachable_restricts_reads_to_own_agent_scope(svc, monkeypatch):
    own_mid = svc.remember(
        SAGE_FOR_DANA, Flow(surface="slack", container="agent:sage", session_id="sess-fm3"),
        content="Note to self: the deploy runbook lives in the platform wiki.",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]
    channel_mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy windows are announced in the channel topic.",
        kind="semantic", origin_kind="explicit_user_ask",
    )["memory_id"]

    def down(*a, **kw):
        raise psycopg.OperationalError("policy tables unreachable")

    monkeypatch.setattr(svc, "_layers", down)
    hits = svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="deploy runbook windows", limit=20)
    ids = {h["id"] for h in hits}
    assert own_mid in ids and channel_mid not in ids
    assert all(h["scope_id"] == "agent:sage" for h in hits)

    read = next(e for e in svc.audit_events(ADMIN, action="READ", actor="agent:sage")
                if own_mid in e["details"]["memory_ids"])
    assert read["details"]["policy_degraded"] is True


async def test_mcp_tools_return_explicit_unavailability(svc, monkeypatch):
    server = build_server(svc, lambda: (SAGE_FOR_DANA, DEPLOYS_FLOW))

    def store_down(*a, **kw):
        raise psycopg.OperationalError("connection refused")

    def policy_down(*a, **kw):
        raise PolicyUnavailable("policy layers unreadable; write refused (doc 07 §5)")

    monkeypatch.setattr(svc, "recall", store_down)
    monkeypatch.setattr(svc, "remember", policy_down)

    for tool, args in (("memory_recall", {"query": "anything"}),
                       ("memory_remember", {"content": "anything"})):
        result = await server.call_tool(tool, args)
        blocks = result[0] if isinstance(result, tuple) else result
        payload = json.loads(blocks[0].text)
        assert payload["unavailable"] is True
        assert payload["say"] == "I can't check my memory right now."


def test_rest_degrades_with_503(svc, monkeypatch):
    client = TestClient(create_app(service=svc))

    def store_down(*a, **kw):
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(svc, "context_block", store_down)
    resp = client.post("/v1/context-block", json={
        "principal": {"agent": "agent:sage", "on_behalf_of": "user:dana"},
        "flow": {"surface": "slack", "container": "channel/C0DEP"},
    })
    assert resp.status_code == 503 and resp.headers["retry-after"] == "30"

    def policy_down(*a, **kw):
        raise PolicyUnavailable("policy layers unreadable; write refused (doc 07 §5)")

    monkeypatch.setattr(svc, "forget", policy_down)
    resp = client.post("/v1/memories/00000000-0000-0000-0000-000000000000/forget",
                       headers={"X-Memoramum-Actor": "agent:sage"}, json={})
    assert resp.status_code == 503
    assert "doc 07" in resp.json()["detail"]
