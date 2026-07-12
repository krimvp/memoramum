"""The streamable-HTTP MCP facade (ADR-0015): the seven tools served
remotely at /mcp on the REST deployment, authenticated with ADR-0014's
principal-bound bearer tokens; the flow context arrives as X-Memoramum-*
headers instead of the stdio launcher's environment.

Runs a real uvicorn server (the transport is the thing under test), with
Sage acting for dana in a scenario-private channel under the seeded
workspace — so the shared fixtures (and the store-derived metrics of
test_observability) stay untouched."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from conftest import SYSTEM, TEST_DB

from memoramum.config import Settings, parse_api_tokens
from memoramum.rest import create_app
from memoramum.service import MemoryService

SAGE_TOKEN = "SAGE-HTTP-TOKEN"
ADMIN_TOKEN = "ADMIN-HTTP-TOKEN"

CHANNEL = "channel/scn-HTTP"

FLOW_HEADERS = {
    "X-Memoramum-On-Behalf-Of": "user:dana",
    "X-Memoramum-Surface": "slack",
    "X-Memoramum-Container": CHANNEL,
    "X-Memoramum-Participants": "user:dana,user:li",
    "X-Memoramum-Session": "sess-http",
}

INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "test", "version": "0"}},
}
POST_ACCEPT = {"Accept": "application/json, text/event-stream",
               "Content-Type": "application/json"}


def _serve(app) -> tuple[uvicorn.Server, threading.Thread, str]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("uvicorn did not start")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    return server, thread, f"http://127.0.0.1:{port}/mcp"


@pytest.fixture(scope="module")
def slice_(svc):
    """A scenario-private channel under the seeded workspace."""
    svc.create_scope(SYSTEM, scope_id=CHANNEL, family="container",
                     parent_scope_id="workspace/T024B", surface="slack")
    for user in ("user:dana", "user:li"):
        svc.set_relation(SYSTEM, CHANNEL, "member", user)
    for rel in ("reader_agent", "writer_agent"):
        svc.set_relation(SYSTEM, CHANNEL, rel, "agent:sage")


@pytest.fixture(scope="module")
def authed(pool, svc, slice_):
    settings = Settings(
        database_url=TEST_DB, embedder="hash",
        api_tokens=parse_api_tokens(
            f"agent:sage={SAGE_TOKEN},user:admin={ADMIN_TOKEN}"),
    )
    tokened = MemoryService(pool, settings)
    server, thread, url = _serve(create_app(service=tokened, settings=settings))
    yield url, tokened
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def dev_shim(pool, svc, slice_):
    settings = Settings(database_url=TEST_DB, embedder="hash", api_tokens=())
    shim = MemoryService(pool, settings)
    server, thread, url = _serve(create_app(service=shim, settings=settings))
    yield url
    server.should_exit = True
    thread.join(timeout=5)


async def _call(url: str, headers: dict, tool: str, args: dict):
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
    if result.structuredContent is not None:
        # FastMCP wraps list-shaped returns as {"result": [...]}
        return result.structuredContent.get("result", result.structuredContent)
    text = result.content[0].text
    return json.loads(text) if text.lstrip().startswith(("{", "[")) else text


async def test_tool_surface_over_http(authed):
    url, _ = authed
    headers = {"Authorization": f"Bearer {SAGE_TOKEN}", **FLOW_HEADERS}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
    assert tools == {
        "memory_remember", "memory_recall", "memory_reinforce", "memory_forget",
        "memory_promote", "memory_confirm", "memory_observe", "memory_status",
    }


async def test_remember_then_recall_with_token_named_actor(authed, pool):
    url, _ = authed
    headers = {"Authorization": f"Bearer {SAGE_TOKEN}", **FLOW_HEADERS}
    verdict = await _call(url, headers, "memory_remember", {
        "content": "The quarterly failover drill runbook lives in the team wiki (HTTP-facade test)",
        "origin_kind": "explicit_user_ask",
        "justification": "dana asked to note it",
    })
    assert verdict["decision"] == "allow"
    memory_id = verdict["memory_id"]

    results = await _call(url, headers, "memory_recall", {
        "query": "failover drill runbook HTTP-facade", "limit": 5})
    assert memory_id in {m["id"] for m in results}

    # ADR-0014 over MCP: the event actor is the token's principal — proven,
    # not asserted — and the asserted on_behalf_of rides along.
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT actor, on_behalf_of FROM memory_events"
            " WHERE memory_id=%s AND action='PROPOSE'", (memory_id,),
        ).fetchone()
    assert row["actor"] == "agent:sage"
    assert row["on_behalf_of"] == "user:dana"


async def test_flow_headers_reach_the_scope_chain(authed):
    """The container header places the write exactly as the stdio env would."""
    url, svc = authed
    headers = {"Authorization": f"Bearer {SAGE_TOKEN}", **FLOW_HEADERS}
    verdict = await _call(url, headers, "memory_remember", {
        "content": "Oncall handover notes are kept in the shared calendar (header-flow test)",
        "origin_kind": "explicit_user_ask",
        "justification": "dana asked",
    })
    mem = svc.status(SYSTEM, memory_id=verdict["memory_id"])["memory"]
    assert mem["scope_id"] == CHANNEL


def test_missing_token_is_401(authed):
    url, _ = authed
    r = httpx.post(url, json=INITIALIZE, headers={**POST_ACCEPT, **FLOW_HEADERS})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_unknown_token_is_401(authed):
    url, _ = authed
    r = httpx.post(url, json=INITIALIZE, headers={
        **POST_ACCEPT, **FLOW_HEADERS, "Authorization": "Bearer WRONG"})
    assert r.status_code == 401


def test_asserted_actor_must_match_token(authed):
    url, _ = authed
    r = httpx.post(url, json=INITIALIZE, headers={
        **POST_ACCEPT, **FLOW_HEADERS,
        "Authorization": f"Bearer {SAGE_TOKEN}",
        "X-Memoramum-Actor": "agent:marge"})
    assert r.status_code == 403


def test_invalid_principal_is_400(authed):
    url, _ = authed
    r = httpx.post(url, json=INITIALIZE, headers={
        **POST_ACCEPT,
        "Authorization": f"Bearer {SAGE_TOKEN}",
        "X-Memoramum-On-Behalf-Of": "not-a-principal"})
    assert r.status_code == 400


async def test_dev_shim_uses_asserted_actor(dev_shim):
    """No tokens configured: the header-asserted shim (ADR-0014) — the CLI
    entry point is what fences it to loopback, exactly as for REST."""
    headers = {"X-Memoramum-Actor": "agent:sage", **FLOW_HEADERS}
    results = await _call(dev_shim, headers, "memory_recall",
                          {"query": "failover drill runbook", "limit": 3})
    assert isinstance(results, list)


def test_dev_shim_requires_asserted_actor(dev_shim):
    r = httpx.post(dev_shim, json=INITIALIZE, headers={**POST_ACCEPT})
    assert r.status_code == 401
