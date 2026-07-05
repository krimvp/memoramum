"""Ambient recall (doc 04 §2.1): invariants first, active facts next,
staged clearly fenced; one READ event per block; budget respected."""

from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA, ADMIN

import pytest

from memoramum import policy
from memoramum.config import Settings
from memoramum.service import MemoryService


@pytest.fixture(scope="module")
def block_world(svc):
    """An invariant, an active fact, and a staged item in #deploys."""
    active = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy summaries are posted in the deploys channel every Tuesday",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]

    pinned = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Never trigger production deploys from chat commands",
        kind="procedural", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]
    with svc.pool.connection() as conn:
        # Pinning is a privileged action with no P1 tool; set up directly.
        conn.execute("UPDATE memories SET status='invariant' WHERE id=%s", (pinned,))

    # The staged tier exists in the schema and read path from day one; only
    # the P1 *policy* keeps agents from filling it. A stage-verdict layer
    # stands in for P2 here.
    staging = MemoryService(svc.pool, Settings(database_url="unused", embedder="hash"))
    staging.policy_layers = [policy.PolicyLayer(
        name="org", version="test-stage",
        strategies=(policy.Strategy(name="stage-inferred", decision="stage",
                                    origin_kinds=("llm_inferred",)),),
    )]
    staged = staging.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Atlas may be adopting feature flags for deploys",
        kind="semantic", origin_kind="llm_inferred", categories=["process"],
        justification="single observation in thread",
    )["memory_id"]
    return {"active": active, "invariant": pinned, "staged": staged}


def test_block_sections_ordered_and_fenced(svc, block_world):
    out = svc.context_block(SAGE_FOR_DANA, DEPLOYS_FLOW, focus="deploys", token_budget=1200)
    block = out["block"]
    assert block.startswith('<memoramum scope="#deploys"')
    i_inv = block.index("INVARIANTS")
    i_facts = block.index("FACTS (current)")
    i_staged = block.index("STAGED (unconfirmed — verify before relying on these)")
    assert i_inv < i_facts < i_staged
    assert "Never trigger production deploys" in block
    assert "[staged; " in block
    assert set(block_world.values()) <= set(out["memory_ids"])

    reads = svc.audit_events(ADMIN, action="READ")
    blk = next(e for e in reads if e["details"].get("context_block_id") == out["block_id"])
    assert blk["details"]["path"] == "ambient"
    assert set(out["memory_ids"]) == set(blk["details"]["memory_ids"])


def test_block_respects_token_budget(svc, block_world):
    small = svc.context_block(SAGE_FOR_DANA, DEPLOYS_FLOW, token_budget=15)
    full = svc.context_block(SAGE_FOR_DANA, DEPLOYS_FLOW, token_budget=1200)
    assert len(small["memory_ids"]) < len(full["memory_ids"])
    # Invariants come first, budget permitting first (doc 03 §1).
    if small["memory_ids"]:
        assert small["memory_ids"][0] == block_world["invariant"]


def test_staged_ranks_below_active(svc, block_world):
    hits = svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query="deploys", limit=20)
    ids = [h["id"] for h in hits]
    assert ids.index(block_world["staged"]) > ids.index(block_world["active"])
