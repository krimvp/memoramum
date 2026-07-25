"""OAuth 2.1 login at the remote facades (ADR-0016): the MCP endpoint as
a resource server — RFC 9728 discovery, the 401 challenge that starts the
login, JWT verification against the authorization server's keys, and the
claim → principal mapping that keeps every audited actor a proven one."""

import json
import time
from types import SimpleNamespace

import jwt
import pytest
from conftest import ADMIN, TEST_DB
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient as RestTestClient
from starlette.testclient import TestClient

from memoramum.config import Settings, parse_api_tokens
from memoramum.mcp_server import create_mcp_app
from memoramum.oauth import (
    AccessTokenVerifier,
    OAuthError,
    OAuthSettings,
    looks_like_jwt,
    www_authenticate,
)
from memoramum.rest import create_app

ISSUER = "https://idp.acme.example"
RESOURCE = "https://memory.acme.example/mcp"
METADATA_URL = "https://memory.acme.example/.well-known/oauth-protected-resource/mcp"
CLIENT_ID = "cli-9f3"
STATIC_TOKEN = "marge-static-t0ken"

MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}
# The flow is asserted per request as ever (ADR-0015); the principal pair
# is what the access token proves.
FLOW_HEADERS = {
    "x-memoramum-surface": "slack",
    "x-memoramum-container": "channel/C0DEP",
    "x-memoramum-participants": "user:dana,user:li",
    "x-memoramum-session": "sess-oauth",
}

OAUTH = OAuthSettings(
    issuer=ISSUER,
    resource=RESOURCE,
    required_scopes=("memoramum.mcp",),
    client_agents=((CLIENT_ID, "agent:sage"),),
)

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _Keys:
    """Stands in for the JWKS client: the authorization server's public
    key, without a network round trip."""

    def get_signing_key_from_jwt(self, token):
        return SimpleNamespace(key=_KEY.public_key())


def verifier_for(settings: OAuthSettings = OAUTH) -> AccessTokenVerifier:
    return AccessTokenVerifier(settings, jwk_client=_Keys())


def mint(**overrides) -> str:
    """An access token as the deployment's authorization server would
    issue it: audience = the resource indicator the client requested."""
    now = int(time.time())
    claims = {
        "iss": ISSUER, "aud": RESOURCE, "sub": "dana", "azp": CLIENT_ID,
        "scope": "memoramum.mcp", "iat": now, "exp": now + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, _KEY, algorithm="RS256")


def auth(token: str) -> dict:
    return {"authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def settings():
    # OAuth and static tokens side by side: one door, two credential kinds.
    return Settings(database_url=TEST_DB, embedder="hash", oauth=OAUTH,
                    api_tokens=parse_api_tokens(f"agent:marge={STATIC_TOKEN}"))


@pytest.fixture(scope="module")
def client(svc, settings):
    with TestClient(create_mcp_app(service=svc, settings=settings,
                                   verifier=verifier_for())) as c:
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


def _post(client, headers):
    return client.post("/mcp", headers={**MCP_HEADERS, **headers},
                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})


# -- discovery: how a client learns where to log in ----------------------

def test_protected_resource_metadata_is_public(client):
    for path in ("/.well-known/oauth-protected-resource/mcp",
                 "/.well-known/oauth-protected-resource"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        document = resp.json()
        assert document["resource"] == RESOURCE
        assert document["authorization_servers"] == [ISSUER]
        assert document["scopes_supported"] == ["memoramum.mcp"]
        assert document["bearer_methods_supported"] == ["header"]


def test_anonymous_call_is_challenged_with_the_metadata_pointer(client):
    resp = _post(client, {})
    assert resp.status_code == 401
    challenge = resp.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert f'resource_metadata="{METADATA_URL}"' in challenge


def test_static_deployments_keep_the_bare_challenge():
    # Nothing to discover without an authorization server (ADR-0014).
    assert www_authenticate(None) == "Bearer"
    assert www_authenticate(OAuthSettings()) == "Bearer"


# -- the login proves the pair ------------------------------------------

def test_authorized_token_names_the_agent_and_proves_the_user(client, svc):
    verdict, err = _call(client, "memory_remember", {
        "content": "Release notes go out the morning after a code freeze lifts",
        "kind": "semantic",
        "origin_kind": "explicit_user_ask",
        "justification": "dana asked to keep this",
    }, {**auth(mint()), **FLOW_HEADERS})
    assert err is None
    assert verdict["decision"] == "allow" and verdict["status"] == "active"

    # The client mapping named the actor; the user who authorized the
    # token is the on-behalf-of — no X-Memoramum-On-Behalf-Of was sent.
    events = svc.audit_events(ADMIN, memory_id=verdict["memory_id"])
    assert all(e["actor"] == "agent:sage" for e in events)
    assert any(e["on_behalf_of"] == "user:dana" for e in events)

    # Leave #deploys as we found it: the seeded world is shared across the
    # suite, and a live memory here competes in every later recall there.
    from conftest import DANA, DEPLOYS_FLOW
    svc.forget(DANA, DEPLOYS_FLOW, memory_id=verdict["memory_id"],
               reason="oauth fixture cleanup")


def test_proven_user_beats_an_asserted_one(client):
    _, err = _call(client, "memory_recall", {"query": "release notes"},
                   {**auth(mint()), **FLOW_HEADERS,
                    "x-memoramum-on-behalf-of": "user:li"})
    assert err is not None
    assert "does not match the user the access token was authorized by" in err

    # Agreeing with the token is fine.
    found, err = _call(client, "memory_recall", {"query": "release notes"},
                       {**auth(mint()), **FLOW_HEADERS,
                        "x-memoramum-on-behalf-of": "user:dana"})
    assert err is None and isinstance(found, list)


def test_asserted_actor_must_match_the_token(client):
    _, err = _call(client, "memory_recall", {"query": "anything"},
                   {**auth(mint()), **FLOW_HEADERS,
                    "x-memoramum-actor": "agent:marge"})
    assert err is not None and "does not match the token's principal" in err


def test_static_tokens_still_work_beside_oauth(client):
    resp = _post(client, auth(STATIC_TOKEN))
    assert resp.status_code == 200


# -- what the door refuses ----------------------------------------------

@pytest.mark.parametrize("claims, expect", [
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"aud": "https://someone-else.example/mcp"}, "Audience"),
    ({"iss": "https://evil.example"}, "Issuer"),
])
def test_bad_tokens_are_refused(client, claims, expect):
    resp = _post(client, auth(mint(**claims)))
    assert resp.status_code == 401
    assert expect.lower() in resp.json()["detail"].lower()
    assert 'error="invalid_token"' in resp.headers["www-authenticate"]


def test_foreign_signature_is_refused(client):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    token = jwt.encode({"iss": ISSUER, "aud": RESOURCE, "sub": "dana", "azp": CLIENT_ID,
                        "scope": "memoramum.mcp", "iat": now, "exp": now + 300},
                       other, algorithm="RS256")
    assert _post(client, auth(token)).status_code == 401


def test_missing_scope_is_insufficient_scope(client):
    resp = _post(client, auth(mint(scope="openid profile")))
    assert resp.status_code == 403
    assert 'error="insufficient_scope"' in resp.headers["www-authenticate"]


def test_unenrolled_client_is_refused(client):
    resp = _post(client, auth(mint(azp="cli-unknown")))
    assert resp.status_code == 401
    assert "not enrolled" in resp.json()["detail"]


def test_discovery_stays_public_behind_the_closed_door(client):
    # The challenge is useless if fetching what it points at needs a token.
    assert client.get("/.well-known/oauth-protected-resource/mcp").status_code == 200


# -- claim mapping (unit) ------------------------------------------------

def test_subject_acts_for_itself_without_a_client_mapping():
    v = verifier_for(OAuthSettings(issuer=ISSUER, resource=RESOURCE))
    credential = v.verify(mint())
    assert credential.actor == "user:dana"
    assert credential.on_behalf_of is None
    assert credential.source == "oauth"


def test_machine_token_carries_no_user():
    v = verifier_for()
    credential = v.verify(mint(sub=CLIENT_ID))
    assert credential.actor == "agent:sage"
    assert credential.on_behalf_of is None


def test_an_idp_that_knows_principals_names_the_actor_outright():
    v = verifier_for(OAuthSettings(issuer=ISSUER, resource=RESOURCE))
    credential = v.verify(mint(memoramum_actor="agent:marge", sub="user:dana"))
    assert credential.actor == "agent:marge"
    assert credential.on_behalf_of == "user:dana"


def test_a_non_user_subject_is_refused():
    v = verifier_for()
    with pytest.raises(OAuthError, match="not a user principal"):
        v.verify(mint(sub="system:ingest"))


def test_credential_shape_picks_the_resolver():
    assert looks_like_jwt(mint())
    assert not looks_like_jwt(STATIC_TOKEN)


# -- key discovery -------------------------------------------------------

def test_jwks_url_is_discovered_from_the_authorization_server():
    seen: list[str] = []

    def http_get(url: str) -> dict:
        seen.append(url)
        if url.endswith("/.well-known/oauth-authorization-server"):
            raise RuntimeError("404")
        return {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/keys"}

    v = AccessTokenVerifier(OAUTH, http_get=http_get)
    assert v._discover_jwks_url() == f"{ISSUER}/keys"
    assert seen[0].endswith("/.well-known/oauth-authorization-server")


def test_metadata_from_a_different_issuer_is_rejected():
    v = AccessTokenVerifier(
        OAUTH, http_get=lambda url: {"issuer": "https://evil.example", "jwks_uri": "x"}
    )
    with pytest.raises(OAuthError, match="cannot discover"):
        v._discover_jwks_url()


# -- the same credential works on the REST facade ------------------------

def test_rest_accepts_the_same_access_token(svc, settings):
    rest = RestTestClient(create_app(service=svc, settings=settings,
                                     verifier=verifier_for()))
    assert rest.get("/healthz").status_code == 200

    resp = rest.get("/v1/metrics")
    assert resp.status_code == 401
    assert f'resource_metadata="{METADATA_URL}"' in resp.headers["WWW-Authenticate"]

    # agent:sage is not the auditor: authenticated, still 403 — the OAuth
    # login establishes a principal, it does not shortcut authorization.
    assert rest.get("/v1/metrics", headers=auth(mint())).status_code == 403
    # user:admin is (conftest seed), reached here through an IdP-named actor.
    admin = mint(memoramum_actor="user:admin", sub="admin")
    assert rest.get("/v1/metrics", headers=auth(admin)).status_code == 200
