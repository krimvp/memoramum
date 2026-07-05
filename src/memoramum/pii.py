"""The PII pipeline (doc 06 §4): analyze → per-entity action → post-action text.

Runs inside the write pipeline's *classify* step (doc 05 §2), before
anything is stored or embedded — vectors never encode what the text
doesn't. Entity analysis is model-shaped work (Presidio-class NER); like
the embedder and the contradiction judge it hides behind a one-method
seam with deterministic local implementations, and the analyzer version
is recorded in provenance `activity` so a reclassification sweep knows
what each memory was scanned with.

Per-entity actions (the doc 06 §4 policy table):

    credential / gov_id   BLOCK the write (deny verdict; the 'credentials'
                          category also trips the org secrets floor —
                          "also enforced by the PII pipeline", doc 05 §1)
    person                TOKENIZE in scopes where the person is not
                          visible anyway (stable per scope via pii_tokens,
                          reversible under privilege); subject
                          self-reference is allowed
    email / phone         REDACT unless the write's categories require
                          contact details

Scope promotion re-runs this pipeline against the destination scope
(doc 03 §4) — tokenization requirements differ between #deploys and
workspace-wide.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from . import scopes

BLOCK_KINDS = ("credential", "gov_id")
REDACT_KINDS = ("email", "phone")


@dataclass(frozen=True)
class Entity:
    kind: str          # 'credential' | 'gov_id' | 'email' | 'phone' | 'person'
    value: str         # the matched surface form ('@dana' → value 'dana')
    start: int
    end: int


class Analyzer(Protocol):
    version: str

    def analyze(self, text: str) -> list[Entity]: ...


class NoneAnalyzer:
    """PII scanning off (dev opt-out); every write passes untouched."""

    version = "none-0"

    def analyze(self, text: str) -> list[Entity]:
        return []


class RegexAnalyzer:
    """Deterministic dev/test stand-in for a Presidio-class analyzer:
    secret-assignment shapes and well-known key formats, gov-id and
    contact-detail patterns, @-mention person references. No NER — a real
    model analyzer slots in behind the same seam; do not deploy."""

    version = "regex-1"

    _PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
        ("credential", re.compile(
            r"(?i)\b(?:password|passwd|token|secret|api[ _-]?key|credential)s?\b"
            r"(?:\s+(?:is|are)|\s*[:=])\s*(?P<v>\S+)")),
        ("credential", re.compile(
            r"\b(?P<v>AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9-]{10,}"
            r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)")),
        ("gov_id", re.compile(r"\b(?P<v>\d{3}-\d{2}-\d{4})\b")),
        ("email", re.compile(r"(?P<v>[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")),
        ("phone", re.compile(r"(?P<v>\+\d[\d() -]{7,}\d)")),
        ("person", re.compile(r"(?<![\w@.])@(?P<v>[A-Za-z0-9._-]*[A-Za-z0-9])")),
    )

    def analyze(self, text: str) -> list[Entity]:
        found: list[Entity] = []
        taken: list[tuple[int, int]] = []
        for kind, pattern in self._PATTERNS:
            for m in pattern.finditer(text):
                span = (m.start("v"), m.end("v")) if kind == "person" else m.span("v")
                if any(s < span[1] and span[0] < e for s, e in taken):
                    continue  # first pattern wins (emails beat @-mentions, …)
                found.append(Entity(kind, m.group("v"), *span))
                taken.append(span)
        return sorted(found, key=lambda e: e.start)


def make_analyzer(name: str) -> Analyzer:
    if name == "none":
        return NoneAnalyzer()
    if name == "regex":
        return RegexAnalyzer()
    raise ValueError(f"unknown PII analyzer {name!r} (expected 'none' or 'regex')")


@dataclass
class PipelineResult:
    content: str                       # the post-action text — embed and store THIS
    blocked: bool = False
    block_reason: str = ""
    categories: list[str] = field(default_factory=list)   # added by classification
    actions: list[dict] = field(default_factory=list)     # provenance record


def _person_principal(value: str) -> str:
    return f"user:{value}"


def _token(cur, scope_id: str, kind: str, value: str) -> str:
    """The stable per-scope pseudonym (doc 06 §4); reversible by reading
    pii_tokens under privilege."""
    row = cur.execute(
        "SELECT token FROM pii_tokens WHERE scope_id=%s AND entity_kind=%s AND value=%s",
        (scope_id, kind, value),
    ).fetchone()
    if row:
        return row["token"]
    n = cur.execute(
        "SELECT count(*) AS n FROM pii_tokens WHERE scope_id=%s AND entity_kind=%s",
        (scope_id, kind),
    ).fetchone()["n"]
    token = f"<{kind.upper()}_{n + 1}>"
    cur.execute(
        "INSERT INTO pii_tokens (scope_id, entity_kind, value, token) VALUES (%s,%s,%s,%s)",
        (scope_id, kind, value, token),
    )
    return token


def run(
    cur,
    entities: list[Entity],
    *,
    content: str,
    target_scope_id: str,
    subjects: list[str],
    effective_user: str | None,
    categories: list[str],
) -> PipelineResult:
    """Apply the per-entity actions against a target scope. Analysis is
    scope-independent (done once by the caller); actions are not — this
    runs again on scope promotion, against the destination."""
    result = PipelineResult(content=content)
    replacements: list[tuple[int, int, str]] = []
    for e in entities:
        if e.kind in BLOCK_KINDS:
            result.blocked = True
            result.block_reason = (
                f"{e.kind} detected by the PII pipeline — the write is blocked (doc 06 §4)"
            )
            result.actions.append({"kind": e.kind, "action": "block"})
            if e.kind == "credential" and "credentials" not in result.categories:
                result.categories.append("credentials")
        elif e.kind == "person":
            principal = _person_principal(e.value)
            visible_anyway = (
                principal == effective_user
                or target_scope_id == scopes.subject_scope_id(principal)
                or scopes.user_can_see(cur, principal, target_scope_id)
            )
            if visible_anyway:
                result.actions.append({"kind": "person", "action": "allow"})
                continue
            token = _token(cur, target_scope_id, "person", e.value)
            replacements.append((e.start - 1, e.end, token))  # include the '@'
            result.actions.append({"kind": "person", "action": "tokenize", "token": token})
        elif e.kind in REDACT_KINDS:
            if "contact" in categories:
                result.actions.append({"kind": e.kind, "action": "allow", "why": "category contact"})
                continue
            replacements.append((e.start, e.end, f"[{e.kind.upper()}]"))
            result.actions.append({"kind": e.kind, "action": "redact"})
    for start, end, repl in sorted(replacements, reverse=True):
        result.content = result.content[:start] + repl + result.content[end:]
    return result
