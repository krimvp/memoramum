"""The remote MCP transport (ADR-0015): the central deployment's
streamable-HTTP endpoint — ADR-0014 bearer tokens at the door, the flow
asserted per request in X-Memoramum-* headers."""

import json

import pytest
from conftest import ADMIN, TEST_DB
from starlette.testclient import TestClient

from memoramum.config import Settings, parse_api_tokens
from memoramum.mcp_server import create_mcp_app

SAGE_TOKEN = "sage-remote-secret"
AUTH = {"authorization": f"Bearer {SAGE_TOKEN}"}

MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}

# The harness asserts the flow per request — the same trust stdio extends
# to the launcher's environment variables (ADR-0015).
FLOW_HEADERS = {
    "x-memoramum-on-behalf-of": "user:dana",
    "x-memoramum-surface": "slack",
    "x-memoramum-container": "channel/C0DEP",
    "x-memoramum-participants": "user:dana,user:li",
    "x-memoramum-session": "sess-remote",
}


@pytest.fixture(scope="module")
def client(svc):
    settings = Settings(database_url=TEST_DB, embedder="hash",
                        api_tokens=parse_api_tokens(f"agent:sage={SAGE_TOKEN}"))
    with TestClient(create_mcp_app(service=svc, settings=settings)) as c:
        yield c


@pytest.fixture(scope="module")
def dev_client(svc):
    settings = Settings(database_url=TEST_DB, embedder="hash", api_tokens=())
    # Dev mode keeps the SDK's loopback-only Host allowlist (DNS-rebinding
    # protection) — so the client must look like a loopback caller.
    app = create_mcp_app(service=svc, settings=settings)
    with TestClient(app, base_url="http://127.0.0.1:8386") as c:
        yield c


def _rpc(client, method, params, headers=None):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        headers={**MCP_HEADERS, **(headers or {})},
    )
    assert resp.status_code == 200, resp.text
    if resp.headers["content-type"].startswith("application/json"):
        return resp.json()
    data = [line[len("data: "):] for line in resp.text.splitlines()
            if line.startswith("data: ")]
    return json.loads(data[-1])


def _call(client, tool, arguments, headers=None):
    """Returns (payload, error_text): error_text is None on success."""
    reply = _rpc(client, "tools/call", {"name": tool, "arguments": arguments}, headers)
    result = reply["result"]
    if result.get("isError"):
        return None, result["content"][0]["text"]
    structured = result.get("structuredContent")
    if structured is not None:
        return structured.get("result", structured), None
    return json.loads(result["content"][0]["text"]), None


def test_door_requires_token(client):
    for headers in (MCP_HEADERS, {**MCP_HEADERS, "authorization": "Bearer wrong"}):
        resp = client.post(
            "/mcp", headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Bearer"


def test_tool_surface_matches_stdio(client):
    reply = _rpc(client, "tools/list", {}, AUTH)
    tools = {t["name"] for t in reply["result"]["tools"]}
    assert tools == {"memory_remember", "memory_recall", "memory_reinforce",
                     "memory_forget", "memory_status", "memory_promote",
                     "memory_confirm", "memory_observe"}


def test_remember_recall_binds_token_actor(client, svc):
    verdict, err = _call(client, "memory_remember", {
        "content": "Deploy freezes are announced in #deploys a day ahead",
        "kind": "semantic",
        "origin_kind": "explicit_user_ask",
        "justification": "dana asked to keep this",
    }, {**AUTH, **FLOW_HEADERS})
    assert err is None
    assert verdict["decision"] == "allow" and verdict["status"] == "active"

    # The flow headers routed the write into the asserted container…
    events = svc.audit_events(ADMIN, memory_id=verdict["memory_id"])
    assert any(e["scope_id"] == "channel/C0DEP" for e in events)
    # …and the actor of every event is the token's principal, acting for
    # the header-asserted user (ADR-0014's audit invariant, kept over MCP).
    assert all(e["actor"] == "agent:sage" for e in events)
    assert any(e["on_behalf_of"] == "user:dana" for e in events)

    found, err = _call(client, "memory_recall",
                       {"query": "deploy freeze announcement"},
                       {**AUTH, **FLOW_HEADERS})
    assert err is None
    assert any(m["id"] == verdict["memory_id"] for m in found)


def test_asserted_actor_must_match_token(client):
    _, err = _call(client, "memory_recall", {"query": "anything"},
                   {**AUTH, **FLOW_HEADERS, "x-memoramum-actor": "agent:marge"})
    assert err is not None and "does not match the token's principal" in err


def test_dev_mode_header_shim(dev_client):
    # No tokens configured: the caller asserts its pair via headers, as in
    # the REST facade's dev mode (main() refuses non-loopback binds).
    payload, err = _call(dev_client, "memory_recall", {"query": "deploy freeze"},
                         {**FLOW_HEADERS, "x-memoramum-actor": "agent:sage"})
    assert err is None and isinstance(payload, list)

    _, err = _call(dev_client, "memory_recall", {"query": "deploy freeze"}, FLOW_HEADERS)
    assert err is not None and "dev mode requires X-Memoramum-Actor" in err
