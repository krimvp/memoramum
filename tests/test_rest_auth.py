"""Bearer-token authentication on the REST facade (ADR-0014, doc 04 §5):
the token — not a caller-asserted header — names the actor."""

import pytest
from fastapi.testclient import TestClient

from memoramum.config import Settings, parse_api_tokens
from memoramum.rest import create_app

SAGE_TOKEN = "sage-t0ken"
ADMIN_TOKEN = "admin-t0ken"
TOKENS = (
    ("agent:sage", SAGE_TOKEN),
    ("user:admin", ADMIN_TOKEN),
)


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def client(svc):
    return TestClient(create_app(service=svc, settings=Settings(api_tokens=TOKENS)))


def test_healthz_stays_open(client):
    assert client.get("/healthz").status_code == 200


def test_missing_token_is_401(client):
    resp = client.get("/v1/metrics")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_unknown_token_and_wrong_scheme_are_401(client):
    assert client.get("/v1/metrics", headers=bearer("nope")).status_code == 401
    assert client.get("/v1/metrics",
                      headers={"Authorization": f"Basic {ADMIN_TOKEN}"}).status_code == 401


def test_token_names_the_actor(client):
    # user:admin is the org auditor (conftest seed) — its token reaches metrics…
    assert client.get("/v1/metrics", headers=bearer(ADMIN_TOKEN)).status_code == 200
    # …while an authenticated-but-unprivileged principal is still 403:
    # authentication does not shortcut authorization.
    assert client.get("/v1/metrics", headers=bearer(SAGE_TOKEN)).status_code == 403


def test_asserted_actor_must_match_token(client):
    headers = {**bearer(SAGE_TOKEN), "X-Memoramum-Actor": "user:admin"}
    assert client.get("/v1/metrics", headers=headers).status_code == 403
    headers = {**bearer(ADMIN_TOKEN), "X-Memoramum-Actor": "user:admin"}
    assert client.get("/v1/metrics", headers=headers).status_code == 200


def test_on_behalf_of_stays_caller_asserted(client, svc):
    from conftest import DEPLOYS_FLOW, SAGE_FOR_DANA
    mid = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Deploy dashboards live in the team wiki",
        kind="semantic", origin_kind="explicit_user_ask", subjects=["user:dana"],
    )["memory_id"]
    ok = client.get(f"/v1/memories/{mid}",
                    headers={**bearer(SAGE_TOKEN), "X-Memoramum-On-Behalf-Of": "user:dana"})
    assert ok.status_code == 200
    # The doc 05 §4.1 intersection rule still fences the claim: eve is not
    # a channel member, so sage reading for her is denied.
    denied = client.get(f"/v1/memories/{mid}",
                        headers={**bearer(SAGE_TOKEN), "X-Memoramum-On-Behalf-Of": "user:eve"})
    assert denied.status_code == 403


def test_context_block_principal_must_match_token(client):
    flow = {"surface": "slack", "container": "channel/C0DEP",
            "participants": ["user:dana", "user:li"]}
    mismatched = client.post("/v1/context-block", headers=bearer(ADMIN_TOKEN), json={
        "principal": {"agent": "agent:sage", "on_behalf_of": "user:dana"}, "flow": flow,
    })
    assert mismatched.status_code == 403

    matched = client.post("/v1/context-block", headers=bearer(SAGE_TOKEN), json={
        "principal": {"agent": "agent:sage", "on_behalf_of": "user:dana"}, "flow": flow,
    })
    assert matched.status_code == 200

    # No asserted principal at all: the token alone names the actor.
    bare = client.post("/v1/context-block",
                       headers={**bearer(SAGE_TOKEN), "X-Memoramum-On-Behalf-Of": "user:dana"},
                       json={"flow": flow})
    assert bare.status_code == 200
    assert bare.json()["block"].startswith("<memoramum ")


def test_token_spec_parsing(monkeypatch):
    parsed = parse_api_tokens(" agent:sage=abc , user:admin=x=y ")
    assert parsed == (("agent:sage", "abc"), ("user:admin", "x=y"))
    assert parse_api_tokens("") == ()
    with pytest.raises(ValueError):
        parse_api_tokens("agent:sage")            # no '=token'
    with pytest.raises(ValueError):
        parse_api_tokens("sage=abc")              # not a valid principal

    monkeypatch.setenv("MEMORAMUM_API_TOKENS", "system:ingest=tok")
    assert Settings().api_tokens == (("system:ingest", "tok"),)
