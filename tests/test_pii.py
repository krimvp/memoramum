"""The PII pipeline (doc 06 §4): credentials block, contact details
redact, person mentions tokenize with stable per-scope pseudonyms, and
scope promotion re-runs the pipeline against the destination."""

from conftest import ADMIN, DANA, DEPLOYS_FLOW, SAGE_FOR_DANA, SYSTEM

from memoramum import pii


def test_analyzer_finds_the_entity_kinds():
    found = pii.RegexAnalyzer().analyze(
        "ping @zara at zara@example.com or +31 20 555 0100; the api key is AKIAABCDEFGHIJKLMNOP"
    )
    kinds = {e.kind for e in found}
    assert kinds == {"person", "email", "phone", "credential"}


def test_credentials_block_the_write_even_uncategorized(svc):
    """The agent supplied no category; the classify step adds
    'credentials' and the org secrets floor denies (doc 05 §1: 'also
    enforced by the PII pipeline')."""
    out = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="the grafana password is s3cr3t-h4sh",
        kind="semantic", origin_kind="explicit_user_ask",
    )
    assert out["decision"] == "deny"
    assert "org floor" in out["reason"]


def test_contact_details_redact_before_store_and_embed(svc):
    out = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="Escalations for deploy incidents go to oncall-atlas@example.com",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )
    mem = svc.get_memory(SAGE_FOR_DANA, out["memory_id"])
    assert mem["content"] == "Escalations for deploy incidents go to [EMAIL]"
    prov = svc.status(ADMIN, memory_id=out["memory_id"])["provenance"]
    assert prov is not None  # actions recorded in provenance activity


def test_person_mentions_tokenize_stably_when_not_visible(svc):
    """@quinn is not a member anywhere near #deploys → tokenized, with the
    same pseudonym on every write into the scope (doc 06 §4)."""
    first = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="@quinn approves deploy exceptions",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )
    second = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="@quinn rotates the deploy captain schedule",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )
    m1 = svc.get_memory(SAGE_FOR_DANA, first["memory_id"])["content"]
    m2 = svc.get_memory(SAGE_FOR_DANA, second["memory_id"])["content"]
    assert m1 == "<PERSON_1> approves deploy exceptions"
    assert m2.startswith("<PERSON_1> ")


def test_members_are_not_tokenized(svc):
    out = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="@dana signs off on hotfix deploys",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )
    mem = svc.get_memory(SAGE_FOR_DANA, out["memory_id"])
    assert mem["content"] == "@dana signs off on hotfix deploys"


def test_promotion_reruns_the_pipeline_against_the_destination(svc):
    """zara is a #deploys member but not visible at workspace level: the
    promoted form is tokenized — a successor derived from the original,
    because content is immutable (ADR-0001)."""
    svc.set_relation(SYSTEM, "channel/C0DEP", "member", "user:zara")
    original = svc.remember(
        SAGE_FOR_DANA, DEPLOYS_FLOW,
        content="@zara owns the deploy calendar",
        kind="semantic", origin_kind="explicit_user_ask", categories=["process"],
    )["memory_id"]
    assert svc.get_memory(SAGE_FOR_DANA, original)["content"].startswith("@zara")

    out = svc.promote(SAGE_FOR_DANA, DEPLOYS_FLOW, memory_id=original,
                      target_scope="workspace/T024B")
    confirmed = svc.confirm_pending(DANA, out["pending_id"], approved=True)
    assert confirmed["transformed"] is True
    promoted = svc.get_memory(SAGE_FOR_DANA, confirmed["memory_id"])
    assert promoted["id"] != original
    assert promoted["scope_id"] == "workspace/T024B"
    assert promoted["content"].endswith("owns the deploy calendar")
    assert promoted["content"].startswith("<PERSON_")
    # The original stays channel-scoped; the promoted form derives from it.
    assert svc.get_memory(SAGE_FOR_DANA, original)["scope_id"] == "channel/C0DEP"
    status = svc.status(ADMIN, memory_id=confirmed["memory_id"])
    assert {"source_type": "memory", "source_id": original} in status["sources"]
