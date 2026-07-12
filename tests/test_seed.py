"""memoramum-seed: provisioning the principals the token config names,
so one MEMORAMUM_API_TOKENS line drives both facade auth (ADR-0014,
ADR-0015) and enrollment."""

import pytest
from conftest import TEST_DB
from starlette.testclient import TestClient
from test_mcp_remote import _call

from memoramum.config import Settings, parse_api_tokens
from memoramum.mcp_server import create_mcp_app
from memoramum.principals import Principal, PrincipalError
from memoramum.seed import seed

TOKENS = "user:seed-anton=T-ANTON,agent:seed-cc=T-CC,agent:seed-cx=T-CX"
PROJECT = "project/seed-repo"


def _principals(settings):
    names = [p for p, _ in settings.api_tokens]
    return ([p for p in names if p.startswith("user:")],
            [p for p in names if p.startswith("agent:")])


@pytest.fixture(scope="module")
def seeded(svc):
    settings = Settings(database_url=TEST_DB, embedder="hash",
                        api_tokens=parse_api_tokens(TOKENS))
    users, agents = _principals(settings)
    report = seed(svc, settings, users=users, agents=agents, projects=(PROJECT,))
    return settings, report


def test_seed_provisions_the_token_principals(seeded):
    _, report = seeded
    assert report["users"] == ["user:seed-anton"]
    assert report["agents"] == ["agent:seed-cc", "agent:seed-cx"]
    for scope in ("subject:user/seed-anton", "agent:seed-cc", "agent:seed-cx",
                  "devsession/seed-cc", "devsession/seed-cx", PROJECT):
        assert scope in report["created_scopes"]
    added = {(r["scope"], r["relation"], r["principal"]) for r in report["added_relations"]}
    assert ("subject:user/seed-anton", "owner", "user:seed-anton") in added
    assert ("devsession/seed-cc", "writer_agent", "agent:seed-cc") in added
    assert (PROJECT, "writer_agent", "agent:seed-cx") in added
    assert (PROJECT, "member", "user:seed-anton") in added


def test_seed_is_idempotent(seeded, svc):
    settings, _ = seeded
    users, agents = _principals(settings)
    again = seed(svc, settings, users=users, agents=agents, projects=(PROJECT,))
    assert again["created_scopes"] == [] and again["added_relations"] == []


def test_sole_user_becomes_owner_and_auditor(seeded, svc):
    _, report = seeded
    assert report["owner"] == "user:seed-anton"
    # auditor: the event-log query gate (doc 04 §5) admits the seeded human.
    events = svc.audit_events(Principal("user:seed-anton"), limit=1)
    assert isinstance(events, list)


def test_owner_must_be_a_seeded_user(svc, seeded):
    settings, _ = seeded
    with pytest.raises(PrincipalError):
        seed(svc, settings, users=("user:seed-anton",), agents=(),
             owner="user:seed-stranger")


def test_seeded_agents_work_end_to_end_over_remote_mcp(seeded, svc):
    """The whole story: the token config names the principals, the seed
    enrolls them, and the agents connect through the remote MCP layer —
    nothing else."""
    settings, _ = seeded
    with TestClient(create_mcp_app(service=svc, settings=settings)) as client:
        cc = {"authorization": "Bearer T-CC",
              "x-memoramum-on-behalf-of": "user:seed-anton",
              "x-memoramum-surface": "ide",
              "x-memoramum-container": "devsession/seed-cc",
              "x-memoramum-project": PROJECT}
        verdict, err = _call(client, "memory_remember", {
            "content": "anton reviews seed-repo merge requests on Friday mornings",
            "origin_kind": "explicit_user_ask",
            "justification": "anton asked to keep this",
        }, cc)
        assert err is None
        assert verdict["decision"] == "allow" and verdict["status"] == "active"

        found, err = _call(client, "memory_recall",
                           {"query": "when does anton review merge requests"}, cc)
        assert err is None
        assert any(m["id"] == verdict["memory_id"] for m in found)

        # The second seeded agent has its own devsession and token; its
        # write lands in its own private container, not seed-cc's.
        cx = {"authorization": "Bearer T-CX",
              "x-memoramum-on-behalf-of": "user:seed-anton",
              "x-memoramum-surface": "ide",
              "x-memoramum-container": "devsession/seed-cx",
              "x-memoramum-project": PROJECT}
        verdict2, err = _call(client, "memory_remember", {
            "content": "seed-repo integration tests need a local Postgres",
            "origin_kind": "explicit_user_ask",
            "justification": "anton asked to keep this",
        }, cx)
        assert err is None and verdict2["decision"] == "allow"
