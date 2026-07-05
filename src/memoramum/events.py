"""The append-only event log (doc 02 §5).

Every state change and every read goes through append(). The optional hash
chain (on by default for `subject:*` scopes, ADR/doc 02 §5) commits to the
event envelope — ids and metadata, never memory content — so hard erasure
leaves the chain intact.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

ACTIONS = frozenset(
    {
        "PROPOSE", "CONFIRM", "REJECT", "PROMOTE_STATUS", "PROMOTE_SCOPE",
        "REINFORCE", "SUPERSEDE", "DEPRECATE", "ARCHIVE", "RESTORE",
        "FORGET", "TOMBSTONE", "QUARANTINE",
        "READ", "POLICY_DECISION", "POLICY_CHANGE", "ENROLLMENT_CHANGE",
    }
)


def _canonical(envelope: dict[str, Any]) -> bytes:
    return json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str).encode()


def append(
    cur,
    *,
    actor: str,
    action: str,
    on_behalf_of: str | None = None,
    memory_id: str | None = None,
    scope_id: str | None = None,
    details: dict[str, Any] | None = None,
    hash_chain_families: frozenset = frozenset({"subject"}),
) -> dict[str, Any]:
    """Append one event inside the caller's transaction; returns the row.

    The caller passes an open cursor so the event commits atomically with
    the write it records (ADR-0004's whole point).
    """
    if action not in ACTIONS:
        raise ValueError(f"unknown event action {action!r}")
    details = details or {}
    event_id = str(uuid.uuid4())

    prev_hash = curr_hash = None
    family = None
    if scope_id:
        row = cur.execute("SELECT family FROM scopes WHERE id = %s", (scope_id,)).fetchone()
        family = row["family"] if row else None
    if family in hash_chain_families:
        # Serialize chain extension per scope: read-modify-write on the
        # chain head is racy without a lock.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (scope_id,))
        head = cur.execute(
            "SELECT curr_hash FROM memory_events"
            " WHERE scope_id = %s AND curr_hash IS NOT NULL"
            " ORDER BY seq DESC LIMIT 1",
            (scope_id,),
        ).fetchone()
        prev_hash = bytes(head["curr_hash"]) if head else b""
        envelope = {
            "event_id": event_id,
            "actor": actor,
            "on_behalf_of": on_behalf_of,
            "action": action,
            "memory_id": memory_id,
            "scope_id": scope_id,
            "details": details,
        }
        curr_hash = hashlib.sha256(_canonical(envelope) + prev_hash).digest()
        prev_hash = prev_hash or None

    return cur.execute(
        "INSERT INTO memory_events"
        " (event_id, actor, on_behalf_of, action, memory_id, scope_id, details, prev_hash, curr_hash)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING seq, event_id, at, actor, on_behalf_of, action, memory_id, scope_id, details",
        (event_id, actor, on_behalf_of, action, memory_id, scope_id,
         json.dumps(details), prev_hash, curr_hash),
    ).fetchone()


def verify_chain(cur, scope_id: str) -> bool:
    """Recompute a scope's hash chain from its events; True if intact."""
    rows = cur.execute(
        "SELECT event_id, actor, on_behalf_of, action, memory_id, scope_id, details, prev_hash, curr_hash"
        " FROM memory_events WHERE scope_id = %s AND curr_hash IS NOT NULL ORDER BY seq",
        (scope_id,),
    ).fetchall()
    prev = b""
    for r in rows:
        envelope = {
            "event_id": str(r["event_id"]),
            "actor": r["actor"],
            "on_behalf_of": r["on_behalf_of"],
            "action": r["action"],
            "memory_id": str(r["memory_id"]) if r["memory_id"] else None,
            "scope_id": r["scope_id"],
            "details": r["details"],
        }
        expect = hashlib.sha256(_canonical(envelope) + prev).digest()
        if bytes(r["curr_hash"]) != expect:
            return False
        prev = expect
    return True
