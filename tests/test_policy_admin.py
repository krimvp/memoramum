"""Policy administration (doc 05 §5): versioned documents, POLICY_CHANGE
events with diffs, the user-preference layer end-to-end, read-side
attribute rules (doc 05 §4.2), retention overrides at the TTL sweep
(doc 03 §5), and simulation mode — the P3 exit criterion."""

from datetime import datetime, timedelta, timezone

from conftest import ADMIN, DANA, DEPLOYS_FLOW, SAGE_FOR_DANA, SYSTEM

import pytest

from memoramum.consolidator import Consolidator
from memoramum.principals import Flow, Principal
from memoramum.service import AccessDenied

ROOT = Principal("user:root")
UMA = Principal("user:uma")


def test_policies_version_and_event_their_changes(svc):
    doc = {"policy": "pol-default", "layer": "agent", "applies_to": "agent:pol",
           "learning": {"strategies": [
               {"name": "explicit", "origin_kinds": ["explicit_user_ask"],
                "decision": "allow"}]}}
    v1 = svc.put_policy(ROOT, doc)
    assert v1["version"] == 1
    doc["learning"]["strategies"].append(
        {"name": "notes", "origin_kinds": ["llm_inferred"], "decision": "stage"})
    v2 = svc.put_policy(ROOT, doc)
    assert v2["version"] == 2
    changes = svc.audit_events(ADMIN, action="POLICY_CHANGE", actor="user:root")
    latest = next(e for e in changes if e["details"].get("policy") == "pol-default"
                  and e["details"]["version"] == 2)
    assert latest["details"]["diff"]["strategies_added"] == ["notes"]
    active = [p for p in svc.policies(ROOT) if p["name"] == "pol-default"]
    assert len(active) == 1 and active[0]["version"] == 2


def test_only_org_admins_administer_shared_layers(svc):
    doc = {"policy": "rogue", "layer": "agent", "applies_to": "agent:sage",
           "learning": {"strategies": []}}
    with pytest.raises(AccessDenied):
        svc.put_policy(DANA, doc)


def test_users_own_their_preference_layer(svc):
    """'don't remember things about me from Slack' (doc 05 §2) — uma can
    always tighten what is learned about her, and only about her."""
    with pytest.raises(AccessDenied):
        svc.put_policy(UMA, {"policy": "uma", "layer": "user_pref",
                             "applies_to": "user:dana", "learning": {"strategies": []}})
    svc.put_policy(UMA, {
        "policy": "uma-no-slack", "layer": "user_pref", "applies_to": "user:uma",
        "learning": {"strategies": [
            {"name": "not-about-me-on-slack", "surfaces": ["slack"], "decision": "deny",
             "reason": "uma opted out of being learned about on Slack"}]},
    })
    denied = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="uma prefers to review deploys before lunch",
        kind="semantic", origin_kind="explicit_user_ask",
        subjects=["user:uma"], categories=["preference"],
    )
    assert denied["decision"] == "deny"
    assert denied["rule_id"].startswith("user_pref/not-about-me-on-slack")
    unrelated = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="The deploy captain rotates alphabetically",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )
    assert unrelated["decision"] == "allow"


def test_read_side_attribute_rules(svc):
    """Sensitivity ceilings, trust floors, staged exclusion and category
    deny-lists (doc 05 §4.2) filter candidates before scoring."""
    svc.set_relation(SYSTEM, "channel/C0DEP", "reader_agent", "agent:auditbot")
    svc.put_policy(ROOT, {
        "policy": "auditbot-reads", "layer": "agent", "applies_to": "agent:auditbot",
        "learning": {"strategies": []},
        "read": {"include_staged": False, "sensitivity_ceiling": "internal",
                 "trust_floor": 0.6, "deny_categories": ["hr_confidential"]},
    })
    fine = svc.remember(SAGE_FOR_DANA, DEPLOYS_FLOW,
                        content="Deploy audit spreadsheets live in the shared drive",
                        kind="semantic", origin_kind="explicit_user_ask",
                        categories=["process"])["memory_id"]
    secret = svc.remember(SAGE_FOR_DANA, DEPLOYS_FLOW,
                          content="The deploy audit covers the acquisition rollout",
                          kind="semantic", origin_kind="explicit_user_ask",
                          sensitivity="confidential")["memory_id"]
    hr = svc.remember(SAGE_FOR_DANA, DEPLOYS_FLOW,
                      content="Deploy audit findings feed the performance reviews",
                      kind="semantic", origin_kind="explicit_user_ask",
                      categories=["hr_confidential"])["memory_id"]
    staged = svc.remember(SAGE_FOR_DANA, DEPLOYS_FLOW,
                          content="The deploy audit may move to quarterly cadence",
                          kind="semantic", origin_kind="llm_inferred")["memory_id"]

    sage_sees = {h["id"] for h in svc.recall(SAGE_FOR_DANA, DEPLOYS_FLOW,
                                             query="deploy audit", limit=20)}
    assert {fine, secret, hr, staged} <= sage_sees
    bot_sees = {h["id"] for h in svc.recall(
        Principal("agent:auditbot", "user:dana"), DEPLOYS_FLOW,
        query="deploy audit", limit=20)}
    assert fine in bot_sees
    assert not {secret, hr, staged} & bot_sees


def test_retention_overrides_drive_ttl_and_tombstone(svc):
    """Strategy TTLs set expires_at; a tombstone-mode override hard-forgets
    at expiry (doc 03 §5) — content gone, marker remains."""
    svc.set_relation(SYSTEM, "channel/C0DEP", "reader_agent", "agent:ret")
    svc.set_relation(SYSTEM, "channel/C0DEP", "writer_agent", "agent:ret")
    svc.put_policy(ROOT, {
        "policy": "ret-default", "layer": "agent", "applies_to": "agent:ret",
        "learning": {"strategies": [
            {"name": "explicit", "origin_kinds": ["explicit_user_ask"],
             "decision": "allow", "ttl": "30d"}]},
        "retention": {"overrides": [
            {"categories": ["health"], "ttl": "7d", "expiry_mode": "tombstone"}]},
    })
    ret = Principal("agent:ret", "user:dana")
    plain = svc.remember(ret, DEPLOYS_FLOW,
                         content="The deploy checklist template gets refreshed quarterly",
                         kind="semantic", origin_kind="explicit_user_ask")["memory_id"]
    health = svc.remember(ret, DEPLOYS_FLOW,
                          content="dana was out with the flu during the March freeze",
                          kind="semantic", origin_kind="explicit_user_ask",
                          subjects=["user:dana"], categories=["health"])["memory_id"]
    rows = {m["id"]: m for m in svc.status(DANA, scope="channel/C0DEP")["memories"]}
    plain_exp = datetime.fromisoformat(rows[plain]["expires_at"])
    health_exp = datetime.fromisoformat(rows[health]["expires_at"])
    now = datetime.now(timezone.utc)
    assert timedelta(days=29) < plain_exp - now < timedelta(days=31)
    assert timedelta(days=6) < health_exp - now < timedelta(days=8)  # override shortens

    swept = Consolidator(svc).sweep(now + timedelta(days=40))
    assert plain in swept["ttl"] and health in swept["ttl_tombstoned"]
    tombstones = svc.audit_events(ADMIN, action="TOMBSTONE", memory_id=health)
    assert tombstones, "a content-free TOMBSTONE event remains"
    with svc.pool.connection() as conn:
        row = conn.execute("SELECT content, content_embedding, status FROM memories"
                           " WHERE id=%s", (health,)).fetchone()
    assert row["status"] == "tombstoned" and row["content"] == ""
    assert row["content_embedding"] is None


def test_simulation_mode_previews_a_policy_change(svc):
    """Doc 05 §5: a proposed policy is evaluated against the recent
    POLICY_DECISION events and reports what would have changed — here,
    sage's staged llm_inferred writes would start being denied."""
    proposal = {
        "policy": "sage-lockdown", "layer": "agent", "applies_to": "agent:sage",
        "learning": {"strategies": [
            {"name": "explicit-only", "origin_kinds": ["explicit_user_ask"],
             "decision": "allow"},
            {"name": "no-inference", "origin_kinds": ["llm_inferred"],
             "decision": "deny", "reason": "locked down for the drill"}]},
    }
    with pytest.raises(AccessDenied):
        svc.simulate_policy(DANA, proposal)
    report = svc.simulate_policy(ADMIN, proposal, days=30)
    assert report["evaluated"] > 0
    assert report["summary"].get("stage->deny", 0) >= 1
    flipped = [c for c in report["changed"] if c["actor"] == "agent:sage"]
    assert flipped and all(c["new"] == "deny" for c in flipped
                           if c["new_rule"].startswith("agent/no-inference"))
    # Nothing was activated: sage still stages inferred writes.
    still = svc.remember(SAGE_FOR_DANA, DEPLOYS_FLOW,
                         content="Deploy dry-runs happen in the staging workspace",
                         kind="semantic", origin_kind="llm_inferred")
    assert still["decision"] == "stage"
