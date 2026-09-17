"""Lifecycle rules (doc 03): reinforcement, staged→active promotion, and
the contradiction judge.

This module holds the *rules*; the service (write path, `memory_reinforce`)
and the consolidator (batch jobs, doc 07 §2) apply them and emit the
events. Judging whether two memories state conflicting facts is LLM-shaped
work; like the embedder, it hides behind a one-method interface with
deterministic local implementations for dev and tests. The System One
judge (`jev`, ADR-0019) is the model judge behind the same seam.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

log = logging.getLogger(__name__)

# Strength increments per reinforcement signal (doc 03 §5): useful
# retrieval +1, re-observation +2, explicit confirm +5. Reinforcement also
# resets t (last_accessed_at).
REINFORCEMENT = {"useful": 1.0, "re_observation": 2.0, "confirm": 5.0}

# Signals agents may send through memory_reinforce (doc 04 §1). The others
# are system-derived: re_observation on the write path, confirm by humans.
AGENT_SIGNALS = ("useful", "wrong")


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


# ---------- the contradiction judge ----------

class Judge(Protocol):
    def judge(self, new_content: str, old_content: str) -> str:
        """'duplicate' | 'contradiction' | 'unrelated'."""
        ...


class ExactJudge:
    """Default: duplicates on normalized equality, never a contradiction.
    Without a model, write-time contradiction detection stays off; the
    'wrong' reinforcement signal and the review queue remain the paths in."""

    def judge(self, new_content: str, old_content: str) -> str:
        return "duplicate" if normalize(new_content) == normalize(old_content) else "unrelated"


class OverlapJudge:
    """Deterministic dev/test stand-in (the HashEmbedder of judges): two
    distinct statements about mostly the same tokens are treated as
    conflicting claims. No semantics — overlap only; do not deploy."""

    threshold = 0.5

    def judge(self, new_content: str, old_content: str) -> str:
        a, b = normalize(new_content), normalize(old_content)
        if a == b:
            return "duplicate"
        ta, tb = set(a.split()), set(b.split())
        if not ta or not tb:
            return "unrelated"
        overlap = len(ta & tb) / min(len(ta), len(tb))
        return "contradiction" if overlap >= self.threshold else "unrelated"


JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_MODEL = "jev-latest"
JEV_KEY_VAR = "TYPESAFE_API_KEY"
JEV_TIMEOUT_SECONDS = 10
# The verdict must reach this confidence before it counts. A false
# contradiction supersedes a good memory (invisible until someone misses
# it); a missed one only delays supersession — the consolidator, the
# 'wrong' signal and the review queue remain the paths in. Asymmetric
# harm, so the gate is high.
JEV_CONFIDENCE_GATE = 0.8

# The one question, in doc 03 §3's terms. `unrelated` is the safe default:
# it is what every fault and every under-confident answer becomes.
JEV_CRITERIA = {
    "duplicate": "Both memories state the same fact; the new memory only restates "
                 "the old one (same subject, same claim), possibly in other words.",
    "contradiction": "Both memories are about the same subject and both cannot hold "
                     "at once; if the new memory is true, the old memory is no "
                     "longer true and the new one supersedes it.",
    "unrelated": "The new memory states a different fact: another subject, or a "
                 "claim that can hold together with the old one.",
}
JEV_INSTRUCTIONS = (
    "Two memories of an AI agent about a team or a person. Compare new_memory "
    "with old_memory: does the new memory restate the old one, contradict it, "
    "or state a different fact?"
)


class JevJudge:
    """The System One judge (ADR-0019): one `choice` question over the pair,
    answered with calibrated probabilities instead of a written verdict.
    Fails closed — no key stops the service at startup; a fault or an
    answer below JEV_CONFIDENCE_GATE is 'unrelated', so the write continues
    exactly as with the exact judge. The evidence of the last verdict on
    this thread (probabilities, confidence, model id, usage) is kept in
    `last_evidence` so the write path can record it (PROPOSE details)."""

    def __init__(self, api_key: str, url: str = JEV_URL):
        self.api_key = api_key
        self.url = url
        self._local = threading.local()

    @property
    def last_evidence(self) -> dict[str, Any] | None:
        return getattr(self._local, "evidence", None)

    def judge(self, new_content: str, old_content: str) -> str:
        self._local.evidence = None
        body = json.dumps({
            "model": JEV_MODEL,
            "state": {"old_memory": old_content, "new_memory": new_content},
            "questions": {"relation": {"type": "choice", "instructions": JEV_INSTRUCTIONS,
                                       "criteria": JEV_CRITERIA}},
        }).encode()
        req = urllib.request.Request(
            self.url, data=body, method="POST",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=JEV_TIMEOUT_SECONDS) as resp:
                answer = parse_jev_answer(json.loads(resp.read()))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            # URLError covers HTTPError (401/422/429/529); ValueError covers
            # bad JSON and every schema mismatch parse_jev_answer raises.
            log.warning("jev judge unavailable, verdict falls to 'unrelated': %s", e)
            return "unrelated"
        self._local.evidence = answer
        if answer["confidence"] < JEV_CONFIDENCE_GATE:
            return "unrelated"
        return answer["choice"]


def parse_jev_answer(data: Any) -> dict[str, Any]:
    """Explicit shape check of the System One response; ValueError on any
    mismatch. Returns the evidence record: choice, confidence,
    probabilities, model, usage."""
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ValueError("response has no 'answers' object")
    ans = data["answers"].get("relation")
    if not isinstance(ans, dict) or ans.get("type") != "choice":
        raise ValueError("answer 'relation' is not a choice answer")
    choice, confidence, probs = ans.get("choice"), ans.get("confidence"), ans.get("probabilities")
    if choice not in JEV_CRITERIA:
        raise ValueError(f"unknown choice {choice!r}")
    if not _is_probability(confidence):
        raise ValueError(f"confidence {confidence!r} is not a probability")
    if (not isinstance(probs, dict) or set(probs) != set(JEV_CRITERIA)
            or not all(_is_probability(v) for v in probs.values())):
        raise ValueError("probabilities do not cover exactly the three criteria")
    model, usage = data.get("model"), data.get("usage")
    if not isinstance(model, str) or not model:
        raise ValueError("response names no model")
    if not isinstance(usage, dict):
        raise ValueError("response has no usage")
    return {"judge": "jev", "choice": choice, "confidence": float(confidence),
            "probabilities": {k: float(v) for k, v in probs.items()},
            "model": model, "usage": usage}


def _is_probability(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0


def make_judge(name: str) -> Judge:
    if name == "exact":
        return ExactJudge()
    if name == "overlap":
        return OverlapJudge()
    if name == "jev":
        key = os.environ.get(JEV_KEY_VAR, "").strip()
        if not key:
            raise ValueError(f"judge 'jev' needs the {JEV_KEY_VAR} environment variable")
        return JevJudge(key)
    raise ValueError(f"unknown judge {name!r} (expected 'exact', 'overlap' or 'jev')")


# ---------- supersede vs hold (doc 03 §3, invariant: staged input can
# ---------- never supersede active memories) ----------

def must_hold(new_status: str, new_trust: float, old: dict[str, Any]) -> bool:
    """True when a contradiction is held for review instead of auto-
    superseding: invariants only change by explicit privileged action, and
    staged/lower-trust claims cannot assassinate active memories
    (doc 06 §3)."""
    if old["status"] == "invariant":
        return True
    if old["status"] == "active":
        return new_status == "staged" or new_trust < old["trust_score"]
    return False


# ---------- the promotion rule (doc 03 §4) ----------

def promotion_due(cur, mem: dict[str, Any], settings, now: datetime | None = None) -> bool:
    """Default rule: promote on (re-observations ≥ 1) OR (useful
    retrievals ≥ 2) OR explicit confirm, with a floor of 3 days in staging
    for agent_observed memories from non-member authors. A memory with an
    unresolved contradiction never rule-promotes ("no contradiction" is
    part of every earned path)."""
    if mem["status"] != "staged":
        return False
    now = now or datetime.now(timezone.utc)

    unresolved = cur.execute(
        "SELECT 1 FROM contradiction_queue WHERE resolved_at IS NULL"
        " AND (challenger_id=%s OR contradicted_id=%s) LIMIT 1",
        (mem["id"], mem["id"]),
    ).fetchone()
    if unresolved:
        return False

    counts = {
        r["signal"]: r["n"]
        for r in cur.execute(
            "SELECT details->>'signal' AS signal, count(*) AS n FROM memory_events"
            " WHERE memory_id=%s AND action='REINFORCE' GROUP BY 1",
            (mem["id"],),
        ).fetchall()
    }
    confirmed = cur.execute(
        "SELECT 1 FROM memory_events WHERE memory_id=%s AND action='CONFIRM' LIMIT 1",
        (mem["id"],),
    ).fetchone()
    earned = (
        counts.get("re_observation", 0) >= settings.promote_reobservations
        or counts.get("useful", 0) >= settings.promote_useful_retrievals
        or confirmed is not None
    )
    if not earned and settings.promote_tenure_days is not None:
        # Unchallenged tenure (optional, off by default): N days staged
        # with ≥1 retrieval and no contradiction.
        earned = (
            mem["access_count"] >= 1
            and now - mem["recorded_at"] >= timedelta(days=settings.promote_tenure_days)
        )
    if not earned:
        return False
    if confirmed is None and _floor_applies(cur, mem):
        return now - mem["recorded_at"] >= timedelta(days=settings.promote_floor_days)
    return True


def _floor_applies(cur, mem: dict[str, Any]) -> bool:
    """agent_observed memories whose source authors are not members of the
    memory's scope (or an ancestor) wait out the staging floor."""
    prov = cur.execute(
        "SELECT origin_kind FROM memory_provenance WHERE memory_id=%s", (mem["id"],)
    ).fetchone()
    if prov is None or prov["origin_kind"] != "agent_observed":
        return False
    authors = [
        r["author"]
        for r in cur.execute(
            "SELECT DISTINCT e.author FROM memory_derivations d"
            " JOIN episodes e ON e.id = d.source_id"
            " WHERE d.memory_id=%s AND d.source_type='episode' AND e.author IS NOT NULL",
            (mem["id"],),
        ).fetchall()
    ]
    if not authors:
        return True  # unattributed sources: treat as non-member (cautious)
    member = cur.execute(
        """
        WITH RECURSIVE up AS (
            SELECT s.* FROM scopes s WHERE s.id = %s
            UNION ALL
            SELECT p.* FROM scopes p JOIN up ON p.id = up.parent_scope_id
        )
        SELECT 1 FROM scope_relations r JOIN up ON up.id = r.scope_id
        WHERE r.relation IN ('member','owner') AND r.principal = ANY(%s) LIMIT 1
        """,
        (mem["scope_id"], authors),
    ).fetchone()
    return member is None


# ---------- re-observation independence (doc 03 §4) ----------

def independent_evidence(cur, memory_id: str, new_episode_ids: list[str]) -> bool:
    """A re-observation counts only from an independent episode: a
    different author or a different day than the evidence already backing
    the memory."""
    if not new_episode_ids:
        return False
    known = cur.execute(
        "SELECT e.author, e.occurred_at::date AS day FROM memory_derivations d"
        " JOIN episodes e ON e.id = d.source_id"
        " WHERE d.memory_id=%s AND d.source_type='episode'",
        (memory_id,),
    ).fetchall()
    authors = {r["author"] for r in known}
    days = {r["day"] for r in known}
    fresh = cur.execute(
        "SELECT author, occurred_at::date AS day FROM episodes WHERE id = ANY(%s::uuid[])",
        (new_episode_ids,),
    ).fetchall()
    return any(r["author"] not in authors or r["day"] not in days for r in fresh)
