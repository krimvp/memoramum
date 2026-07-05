"""The P2 write pipeline: explicit_user_ask lands active with full
provenance, llm_inferred routes to the staged tier (ADR-0003), and
everything later-phase is denied — as an event, not a shrug."""

from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA, ADMIN


def test_explicit_ask_lands_active_with_provenance(svc):
    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Team Atlas deploys to production only on Tuesdays",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["team:atlas"], categories=["process"],
        justification="dana asked Sage to remember the deploy rule",
    )
    assert verdict["decision"] == "allow"
    assert verdict["status"] == "active"          # explicit asks skip staging (doc 03 §2)
    mid = verdict["memory_id"]

    status = svc.status(SAGE_FOR_DANA, memory_id=mid)
    assert status["memory"]["scope_id"] == "channel/C0DEP"   # route: source
    assert status["provenance"]["origin_kind"] == "explicit_user_ask"
    assert status["provenance"]["responsible_agent"] == "agent:sage"
    assert status["provenance"]["on_behalf_of"] == "user:dana"
    # The ask is the episode: provenance chain never starts empty.
    assert len(status["sources"]) == 1
    actions = {d["action"] for d in status["event_digest"]}
    assert "PROPOSE" in actions


def test_inferred_writes_route_to_the_staged_tier(svc):
    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Atlas may be adopting feature flags",
        kind="semantic", origin_kind="llm_inferred",
        justification="single observation in a thread",
    )
    assert verdict["decision"] == "stage"
    assert verdict["status"] == "staged"       # useful immediately, trusted later (ADR-0003)
    assert verdict["memory_id"] is not None


def test_later_phase_origins_denied_in_p2(svc):
    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Background extraction has no write path yet",
        kind="semantic", origin_kind="agent_observed",
    )
    assert verdict["decision"] == "deny"
    assert verdict["memory_id"] is None
    assert "P4" in verdict["reason"]
    # Denials are queryable: "what has agent X been prevented from learning".
    denials = [
        e for e in svc.audit_events(ADMIN, action="POLICY_DECISION", actor="agent:sage")
        if e["details"]["verdict"] == "deny"
    ]
    assert denials


def test_secrets_denied_even_when_explicitly_asked(svc):
    verdict = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="the deploy token is hunter2",
        kind="semantic", origin_kind="explicit_user_ask", categories=["credentials"],
    )
    assert verdict["decision"] == "deny"
    assert "org floor" in verdict["reason"]


def test_unenrolled_agent_cannot_write(svc):
    from memoramum.principals import Principal
    verdict = svc.remember(
        Principal("agent:marge", "user:dana"), DEPLOYS_FLOW,
        content="Marge should not be able to write here",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    assert verdict["decision"] == "deny"
    assert verdict["rule_id"] == "access/enrollment"
