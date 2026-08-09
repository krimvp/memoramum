"""Retrieval scoring (doc 04 §3): the three gated legs of ADR-0016/0017 and
the policy-set score exponents of ADR-0018.

The invariants under test are about *what retrieval refuses to return*: a
focused read that matches nothing must come back empty rather than ranked,
and the pinned tier must survive that emptiness (doc 03 §1).
"""

from dataclasses import replace

from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA

import pytest

from memoramum import policy, retrieval
from memoramum.embedding import make_embedder
from memoramum.scopes import resolve_chain

# Tokens that appear nowhere in the seeded world, in any leg: no shared
# lexemes, no trigram extent, no hash-embedder overlap.
NONSENSE = "capybara husbandry lagomorph burrow"


@pytest.fixture(scope="module")
def code_memory(svc):
    """A codebase convention: the statement is *about* a path and a symbol,
    which is what the english parser folds into single lexemes (ADR-0017)."""
    return svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="payments/ledger.py stores money as integer cents, never floats",
        kind="semantic", origin_kind="explicit_user_ask",
        categories=["codebase_convention"],
    )["memory_id"]


@pytest.fixture(scope="module")
def chain(svc):
    with svc.pool.connection() as conn:
        return resolve_chain(conn.cursor(), SAGE_FOR_DANA, DEPLOYS_FLOW)


def _search(svc, chain, query, *, embedding=None, settings=None, **kw):
    with svc.pool.connection() as conn:
        return retrieval.search(
            conn.cursor(), scope_chain=chain, query=query, query_embedding=embedding,
            settings=settings or svc.settings, **kw,
        )


# ---------- ADR-0016: absolute admission ----------

def test_focused_recall_with_no_match_returns_nothing(svc, code_memory):
    """The empty answer is what lets an agent say "I have nothing on that"
    instead of citing the least-bad row."""
    assert svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW, query=NONSENSE) == []


def test_ambient_block_without_a_focus_still_fills(svc, code_memory):
    """A query-less block has no relevance question to answer, so the
    recency leg stands — continuity is the feature there (doc 04 §2.1)."""
    out = svc.context_block(SAGE_FOR_DANA, DEPLOYS_FLOW, token_budget=1200)
    assert out["memory_ids"]


def test_focused_block_that_matches_nothing_still_delivers_invariants(svc):
    """Doc 03 §1: invariants are always included in ambient recall for their
    scope. They are pinned, not ranked — an unmatchable focus must not take
    them out with the rest."""
    pinned = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Never trigger production deploys from chat commands",
        kind="procedural", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]
    with svc.pool.connection() as conn:
        conn.execute("UPDATE memories SET status='invariant' WHERE id=%s", (pinned,))

    out = svc.context_block(SAGE_FOR_DANA, DEPLOYS_FLOW, focus=NONSENSE, token_budget=1200)
    assert pinned in out["memory_ids"]
    assert "INVARIANTS" in out["block"]
    assert "FACTS (current)" not in out["block"]


def test_vector_leg_admits_only_within_the_distance_ceiling(svc, chain, code_memory):
    """The ceiling — not an absence of neighbours — is what keeps unrelated
    memories out: lift it past 1.0 and the same query returns them."""
    embedding = make_embedder("hash").embed(NONSENSE)
    assert _search(svc, chain, NONSENSE, embedding=embedding) == []

    no_ceiling = replace(svc.settings, vector_distance_ceiling=2.0)
    assert _search(svc, chain, NONSENSE, embedding=embedding, settings=no_ceiling)


# ---------- ADR-0017: the literal leg ----------

def test_literal_leg_finds_identifiers_the_lexical_leg_misses(svc, chain, code_memory):
    """`payments/ledger.py` is one lexeme to the english parser, so the
    stemmed leg cannot match `ledger.py`. The trigram leg can."""
    with svc.pool.connection() as conn:
        lexical = conn.execute(
            "SELECT content_tsv @@ websearch_to_tsquery('english', %s) AS hit"
            " FROM memories WHERE id = %s",
            ("ledger.py", code_memory),
        ).fetchone()["hit"]
    assert lexical is False

    hits = _search(svc, chain, "ledger.py")     # no embedding: lexical ⊕ literal only
    assert code_memory in [str(h["id"]) for h in hits]


def test_literal_leg_stays_quiet_on_prose(svc, chain, code_memory):
    """Word similarity scores the whole query against its best extent, so a
    long question never clears the threshold — the leg adds candidates only
    where the other two are weak."""
    hits = _search(svc, chain, "what is the policy for deploying on fridays")
    assert code_memory not in [str(h["id"]) for h in hits]


def test_camel_case_symbols_are_reachable(svc, chain):
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="The LedgerEntry model is append-only; corrections post a reversing entry",
        kind="semantic", origin_kind="explicit_user_ask",
        categories=["codebase_convention"],
    )["memory_id"]
    assert mid in [str(h["id"]) for h in _search(svc, chain, "LedgerEntry")]


# ---------- ADR-0018: weights are exponents, set by policy ----------

def test_zero_weights_flatten_every_factor(svc, chain, code_memory):
    """0 disables a factor; all five off leaves the bare product 1.0."""
    off = dict.fromkeys(policy.SCORE_WEIGHTS, 0.0)
    hits = _search(svc, chain, "ledger.py", weights=off)
    assert hits and all(h["score"] == pytest.approx(1.0) for h in hits)


def test_status_exponent_sharpens_the_tier_gap(svc, chain, code_memory):
    """status_weight 0.6 for staged becomes 0.6² at weight 2 — the tier gap
    widens without staged being excluded (which is `include_staged`'s job)."""
    staged = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="payments/ledger.py may be moving to a decimal type",
        kind="semantic", origin_kind="llm_inferred",
        categories=["codebase_convention"], justification="single observation",
    )["memory_id"]

    def score_of(weights):
        hits = _search(svc, chain, "ledger.py", weights=weights, limit=20)
        return next(h["score"] for h in hits if str(h["id"]) == staged)

    assert score_of({"status": 2.0}) == pytest.approx(score_of({}) * 0.6)


def test_weights_reach_retrieval_through_the_read_policy(svc):
    """The wiring, not the arithmetic: a policy weight lands on the score."""
    rp = policy.read_policy([
        policy.layer_from_document(policy.parse_document("""
policy: p
layer: agent
applies_to: agent:sage
learning: {default: deny, strategies: []}
read: {weights: {relevance: 0, retention: 0, trust: 0, status: 0, scope_proximity: 0}}
"""), 1),
    ])
    assert rp.weight_map() == dict.fromkeys(policy.SCORE_WEIGHTS, 0.0)


# ---------- ADR-0018: composition ----------

def _layer(name, applies_to, weights):
    return policy.layer_from_document(policy.parse_document(f"""
policy: {name}
layer: {name}
applies_to: {applies_to}
learning: {{default: deny, strategies: []}}
read: {{weights: {weights}}}
"""), 1)


def test_narrowest_layer_wins_below_org():
    layers = [_layer("surface", "surface:slack", "{status: 2.0, trust: 3.0}"),
              _layer("agent", "agent:sage", "{status: 0.5}")]
    assert policy.read_policy(layers).weight_map() == {"status": 0.5, "trust": 3.0}


def test_org_weights_are_final():
    """The org layer is un-overridable (doc 05 principle 3) — what lower
    layers gain is only the freedom the org declined to spend."""
    layers = [_layer("org", "org:acme", "{status: 2.0}"),
              _layer("agent", "agent:sage", "{status: 0.5, trust: 0.5}")]
    assert policy.read_policy(layers).weight_map() == {"status": 2.0, "trust": 0.5}


def test_gates_still_compose_strictest_wins():
    """Weights change how the org floor applies to weights only; the four
    gates keep tightening downward."""
    layers = [
        policy.layer_from_document(policy.parse_document("""
policy: o
layer: org
applies_to: org:acme
learning: {default: deny, strategies: []}
read: {trust_floor: 0.3, weights: {status: 2.0}}
"""), 1),
        policy.layer_from_document(policy.parse_document("""
policy: a
layer: agent
applies_to: agent:sage
learning: {default: deny, strategies: []}
read: {trust_floor: 0.8}
"""), 1),
    ]
    merged = policy.read_policy(layers)
    assert merged.trust_floor == 0.8
    assert merged.weight_map() == {"status": 2.0}


@pytest.mark.parametrize("weights", [
    "{unknown_factor: 1.0}",     # not one of the five doc 04 §3 factors
    "{status: -1.0}",            # a negative exponent would invert the factor
    "{status: 9.0}",             # beyond the bound
    "{status: 'high'}",          # not a number
])
def test_bad_weights_are_rejected_at_parse_time(weights):
    with pytest.raises(policy.PolicyError):
        policy.parse_document(f"""
policy: p
layer: agent
applies_to: agent:sage
learning: {{default: deny, strategies: []}}
read: {{weights: {weights}}}
""")
