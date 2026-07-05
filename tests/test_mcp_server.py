"""The MCP facade: the six doc 04 §1 verbs plus memory_confirm, and the
prompt contract."""

import json

import pytest
from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA

from memoramum.mcp_server import build_server


@pytest.fixture(scope="module")
def server(svc):
    return build_server(svc, SAGE_FOR_DANA, DEPLOYS_FLOW)


async def test_tool_surface(server):
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"memory_remember", "memory_recall", "memory_reinforce",
                     "memory_forget", "memory_status", "memory_promote",
                     "memory_confirm"}
    prompts = {p.name for p in await server.list_prompts()}
    assert "prompt_contract" in prompts


async def test_remember_then_recall_via_tools(server):
    result = await server.call_tool("memory_remember", {
        "content": "The staging environment is refreshed every Monday morning",
        "kind": "semantic",
        "origin_kind": "explicit_user_ask",
        "justification": "dana asked to keep this",
    })
    verdict = _payload(result)
    assert verdict["decision"] == "allow" and verdict["status"] == "active"

    result = await server.call_tool("memory_recall", {"query": "staging environment refresh"})
    contents = _payload(result, many=True)
    assert any(m["id"] == verdict["memory_id"] for m in contents)


def _payload(result, many=False):
    # FastMCP returns content blocks (and, on newer versions, a structured
    # payload alongside). Normalize to plain dicts.
    if isinstance(result, tuple):
        blocks, structured = result
        if structured is not None:
            value = structured.get("result", structured)
            return value
    else:
        blocks = result
    texts = [json.loads(b.text) for b in blocks if getattr(b, "text", None)]
    if many:
        return texts if len(texts) != 1 or isinstance(texts[0], dict) else texts[0]
    return texts[0]
