"""The learning-policy engine (doc 05 §1–2): document parsing, layered
strictest-wins evaluation, routing, and the doc 05 §1 example policy
evaluated against the §6 worked-scenario rows — verbatim."""

import pytest

from memoramum import policy

# The sage-slack-default policy, exactly as printed in doc 05 §1.
DOC05_YAML = """
policy: sage-slack-default
layer: agent
applies_to: agent:sage

learning:
  default: deny
  strategies:
    - name: team-process-facts
      kinds: [semantic]
      categories: [process, tooling, schedule]
      origin_kinds: [agent_observed, llm_inferred]
      route:
        scope: source
      decision: stage
      ttl: null
    - name: explicit-asks
      kinds: [semantic, procedural, profile]
      origin_kinds: [explicit_user_ask]
      decision: allow
    - name: personal-preferences
      kinds: [profile, semantic]
      categories: [preference]
      subjects: participants
      route: { scope: subject }
      decision: stage
      ttl: 365d
    - name: sensitive-personal
      categories: [health, beliefs, relationships, location_history]
      decision: ask
      sensitivity_floor: confidential
    - name: secrets
      categories: [credentials, secrets]
      decision: deny

  procedural_writes: ask

promotion:
  to_shared_scope: ask
  from_private_scope: deny
  confirmers: [scope_member]

retention:
  overrides:
    - { categories: [health], ttl: 90d, expiry_mode: tombstone }

read:
  include_staged: true
  trust_floor: 0.3
  sensitivity_ceiling: internal
  weights:
    status: 2.0
    scope_proximity: 1.0
"""


@pytest.fixture(scope="module")
def sage_layer():
    return policy.layer_from_document(policy.parse_document(DOC05_YAML), 1)


def test_parse_canonicalizes_the_doc05_example(sage_layer):
    assert sage_layer.name == "agent" and sage_layer.applies_to == "agent:sage"
    assert [s.name for s in sage_layer.strategies] == [
        "team-process-facts", "explicit-asks", "personal-preferences",
        "sensitive-personal", "secrets",
    ]
    assert sage_layer.procedural_writes == "ask"
    assert sage_layer.promotion["confirmers"] == ["scope_member"]


def test_scenario_row_1_extraction_stages_to_source(sage_layer):
    """Sage's extraction proposes the Tuesday rule → team-process-facts →
    stage, routed to the source scope (doc 05 §6)."""
    v = policy.evaluate([sage_layer], policy.Candidate(
        kind="semantic", categories=("process",), origin_kind="agent_observed"))
    assert v.decision == "stage"
    assert v.rule_id.startswith("agent/team-process-facts")
    assert v.route_scope == "source"


def test_scenario_row_2_explicit_preference_routes_to_subject(sage_layer):
    """'remember I prefer thread summaries' → explicit-asks allows, but
    routing sends it to subject:user/dana (doc 05 §6)."""
    v = policy.evaluate([sage_layer], policy.Candidate(
        kind="semantic", categories=("preference",), origin_kind="explicit_user_ask",
        subjects=("user:dana",), participants=("user:dana", "user:li")))
    assert v.decision == "allow" and v.rule_id.startswith("agent/explicit-asks")
    assert v.route_scope == "subject"
    assert v.ttl_days == 365.0


def test_scenario_row_3_health_asks_with_a_floor(sage_layer):
    """A colleague's medical leave → sensitive-personal → ask, never
    silent (doc 05 §6)."""
    v = policy.evaluate([sage_layer], policy.Candidate(
        kind="semantic", categories=("health",), origin_kind="llm_inferred",
        subjects=("user:li",)))
    assert v.decision == "ask" and v.rule_id.startswith("agent/sensitive-personal")
    assert v.sensitivity_floor == "confidential"


def test_scenario_row_4_promotion_gate_asks_a_scope_member(sage_layer):
    gate = policy.promotion_gate([sage_layer], crossing="shared",
                                 source_trust_class="internal_public")
    assert gate.decision == "ask" and gate.confirmer == "scope_member"


def test_scenario_row_5_read_policy(sage_layer):
    rp = policy.read_policy([sage_layer])
    assert rp.include_staged is True
    assert rp.trust_floor == 0.3
    assert rp.sensitivity_ceiling == "internal"


def test_no_match_anywhere_is_deny(sage_layer):
    v = policy.evaluate([sage_layer], policy.Candidate(
        kind="episodic", origin_kind="llm_inferred"))
    assert v.decision == "deny" and v.rule_id == "default-deny"


def test_strictest_wins_across_layers(sage_layer):
    """An agent-layer allow cannot beat an org-layer deny (doc 05 §2)."""
    v = policy.evaluate([policy.BUILTIN_ORG_LAYER, sage_layer], policy.Candidate(
        kind="semantic", categories=("credentials",), origin_kind="explicit_user_ask"))
    assert v.decision == "deny"
    assert v.rule_id.startswith("org/secrets")          # the org floor wins the tie
    assert "org floor" in v.reason


def test_user_pref_layer_tightens_but_never_loosens(sage_layer):
    """Users tighten what is learned about them (doc 05 §2)."""
    pref = policy.layer_from_document(policy.parse_document({
        "policy": "dana-no-slack", "layer": "user_pref", "applies_to": "user:dana",
        "learning": {"strategies": [
            {"name": "not-about-me-on-slack", "surfaces": ["slack"], "decision": "deny"},
        ]},
    }), 1)
    about_dana = policy.Candidate(
        kind="semantic", categories=("process",), origin_kind="llm_inferred",
        subjects=("user:dana",), surface="slack")
    assert policy.evaluate([policy.BUILTIN_ORG_LAYER, sage_layer], about_dana).decision == "stage"
    assert policy.evaluate(
        [policy.BUILTIN_ORG_LAYER, sage_layer, pref], about_dana).decision == "deny"
    elsewhere = policy.Candidate(
        kind="semantic", categories=("process",), origin_kind="llm_inferred",
        subjects=("user:dana",), surface="gitlab")
    assert policy.evaluate(
        [policy.BUILTIN_ORG_LAYER, sage_layer, pref], elsewhere).decision == "stage"


def test_procedural_writes_tighten_matched_decisions(sage_layer):
    v = policy.evaluate([sage_layer], policy.Candidate(
        kind="procedural", origin_kind="explicit_user_ask"))
    assert v.decision == "ask"
    assert v.rule_id.startswith("agent/procedural-writes")


def test_promotion_gates_follow_the_doc05_table(sage_layer):
    layers = [sage_layer]
    assert policy.promotion_gate(layers, crossing="narrowing",
                                 source_trust_class="internal_public").decision == "allow"
    private = policy.promotion_gate(layers, crossing="shared", source_trust_class="private")
    assert private.decision == "deny" and "private" in private.reason
    external = policy.promotion_gate(layers, crossing="shared",
                                     source_trust_class="shared_external")
    assert external.decision == "deny"
    subject = policy.promotion_gate(layers, crossing="subject",
                                    source_trust_class="internal_public")
    assert subject.decision == "ask" and subject.confirmer == "subject"


def test_documents_cannot_loosen(sage_layer):
    with pytest.raises(policy.PolicyError):
        policy.parse_document({"policy": "x", "layer": "agent", "applies_to": "agent:x",
                               "learning": {"default": "allow"}})
    with pytest.raises(policy.PolicyError):
        policy.parse_document({"policy": "x", "layer": "agent", "applies_to": "agent:x",
                               "promotion": {"from_private_scope": "allow"}})
    with pytest.raises(policy.PolicyError):
        policy.parse_document({"policy": "x", "layer": "agent", "applies_to": "agent:x",
                               "learning": {"procedural_writes": "allow"}})


def test_retention_override_strictest_wins(sage_layer):
    o = policy.retention_override([sage_layer], ["health", "process"])
    assert o == {"ttl_days": 90.0, "expiry_mode": "tombstone"}
    assert policy.retention_override([sage_layer], ["process"]) is None
