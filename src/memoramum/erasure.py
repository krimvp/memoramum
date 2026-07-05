"""The GDPR erasure pipeline (doc 06 §2.2).

Soft delete is not erasure: the pipeline enumerates the full derived
closure, splits consolidated derivatives, hard-deletes content and
embeddings, tombstones, then verifies and signs an attestation. Events
carry ids and metadata, never content — which is what lets the audit
chain survive the erasure intact (doc 02 §5).

Steps, mapped to doc 06 §2.2:

1. **Enumerate** — `subject_ids` lookup ∪ reverse `memory_derivations`
   walk from the subject's episodes → the derived closure (including
   `consolidated` memories that mixed this subject in).
2. **Split consolidated derivatives** — a consolidated memory with
   surviving inputs is re-generated from them (the same deterministic
   consolidation rule that produced it: `_consolidated_insert`) rather
   than deleted wholesale.
3. **Hard-delete content** — `memories.content`, `content_embedding`
   (the generated tsv follows), episode verbatim copies, and the
   subject's `pii_tokens` rows (detokenization dies with them).
4. **Tombstone** — `status='tombstoned'` plus a content-free TOMBSTONE
   event carrying the legal basis and request id.
5. **Verify & attest** — id-based post-erasure scan and an HMAC-signed
   attestation record stored on the request.

The reverse-derivation walk (`derived_closure`) is shared with the
quarantine lever (doc 06 §3) — both are the doc 02 §4 reverse index at
work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone


def derived_closure(cur, seed_memory_ids: set[str]) -> set[str]:
    """The seed memories plus everything transitively derived from them
    (reverse walk over memory→memory derivation edges, doc 02 §4)."""
    closure = set(seed_memory_ids)
    frontier = sorted(closure)
    while frontier:
        rows = cur.execute(
            "SELECT DISTINCT memory_id FROM memory_derivations"
            " WHERE source_type='memory' AND source_id = ANY(%s::uuid[])",
            (frontier,),
        ).fetchall()
        frontier = [str(r["memory_id"]) for r in rows if str(r["memory_id"]) not in closure]
        closure.update(frontier)
    return closure


def run(svc, cur, *, principal, subject: str, legal_basis: str, note: str) -> dict:
    req = cur.execute(
        "INSERT INTO erasure_requests (subject, legal_basis, requested_by, note)"
        " VALUES (%s,%s,%s,%s) RETURNING id, created_at",
        (subject, legal_basis, principal.actor, note),
    ).fetchone()
    request_id = str(req["id"])

    # 1. Enumerate.
    episode_ids = [str(r["id"]) for r in cur.execute(
        "SELECT id FROM episodes WHERE author=%s", (subject,)
    ).fetchall()]
    seeds = {str(r["id"]) for r in cur.execute(
        "SELECT id FROM memories WHERE %s = ANY(subject_ids)", (subject,)
    ).fetchall()}
    if episode_ids:
        seeds |= {str(r["memory_id"]) for r in cur.execute(
            "SELECT DISTINCT memory_id FROM memory_derivations"
            " WHERE source_type='episode' AND source_id = ANY(%s::uuid[])",
            (episode_ids,),
        ).fetchall()}
    closure = derived_closure(cur, seeds)
    rows = cur.execute(
        "SELECT * FROM memories WHERE id = ANY(%s::uuid[]) ORDER BY recorded_at",
        (sorted(closure),),
    ).fetchall() if closure else []

    # 2. Split consolidated derivatives: re-generate from surviving inputs.
    regenerated = []
    for row in rows:
        prov = cur.execute(
            "SELECT origin_kind FROM memory_provenance WHERE memory_id=%s", (row["id"],)
        ).fetchone()
        if prov is None or prov["origin_kind"] != "consolidated":
            continue
        survivors = cur.execute(
            "SELECT m.* FROM memory_derivations d JOIN memories m ON m.id = d.source_id"
            " WHERE d.memory_id=%s AND d.source_type='memory'"
            " AND NOT (m.id = ANY(%s::uuid[])) AND m.status <> 'tombstoned'",
            (row["id"], sorted(closure)),
        ).fetchall()
        if not survivors or row["status"] not in ("staged", "active", "invariant"):
            continue
        winner = max(survivors, key=lambda m: (m["strength"], m["recorded_at"], str(m["id"])))
        successor_id = svc._consolidated_insert(
            cur, principal, survivors, scope_id=row["scope_id"],
            content=winner["content"], kind=row["kind"], job="erasure-regenerate",
            justification=f"re-generated from remaining sources after erasure of {subject}"
                          " (doc 06 §2.2 step 2)",
            extra_activity={"erasure_request": request_id, "replaced": str(row["id"])},
        )
        regenerated.append({"replaced": str(row["id"]), "successor": successor_id})

    # 3 + 4. Hard-delete content and tombstone, with the content-free marker.
    for row in rows:
        cur.execute(
            "UPDATE memories SET status='tombstoned', content='', content_embedding=NULL"
            " WHERE id=%s",
            (row["id"],),
        )
        svc._event(
            cur, principal, "TOMBSTONE", memory_id=str(row["id"]), scope_id=row["scope_id"],
            details={"from": row["status"], "legal_basis": legal_basis,
                     "erasure_request": request_id},
        )
    episodes_scrubbed = cur.execute(
        "UPDATE episodes SET content=NULL WHERE author=%s AND content IS NOT NULL"
        " RETURNING id",
        (subject,),
    ).fetchall()
    bare = subject.partition(":")[2]
    tokens_dropped = cur.execute(
        "DELETE FROM pii_tokens WHERE entity_kind='person' AND value=%s RETURNING token",
        (bare,),
    ).fetchall()

    # 5. Verify & attest.
    checks = {
        "residual_content": cur.execute(
            "SELECT count(*) AS n FROM memories WHERE id = ANY(%s::uuid[])"
            " AND (content <> '' OR content_embedding IS NOT NULL)",
            (sorted(closure) or None,),
        ).fetchone()["n"] if closure else 0,
        "live_about_subject": cur.execute(
            "SELECT count(*) AS n FROM memories WHERE %s = ANY(subject_ids)"
            " AND status IN ('staged','active','invariant')",
            (subject,),
        ).fetchone()["n"],
        "episode_verbatims": cur.execute(
            "SELECT count(*) AS n FROM episodes WHERE author=%s AND content IS NOT NULL",
            (subject,),
        ).fetchone()["n"],
    }
    verified_at = datetime.now(timezone.utc)
    payload = {
        "request_id": request_id,
        "subject": subject,
        "legal_basis": legal_basis,
        "memories_tombstoned": sorted(closure),
        "episodes_scrubbed": len(episodes_scrubbed),
        "pii_tokens_dropped": len(tokens_dropped),
        "regenerated": regenerated,
        "checks": checks,
        "verified": all(v == 0 for v in checks.values()),
        "verified_at": verified_at.isoformat(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    attestation = {**payload, "signature": hmac.new(
        svc.settings.attestation_key.encode(), canonical, hashlib.sha256
    ).hexdigest()}
    cur.execute(
        "UPDATE erasure_requests SET completed_at=%s, attestation=%s WHERE id=%s",
        (verified_at, json.dumps(attestation), request_id),
    )
    return {"request_id": request_id, "subject": subject, "legal_basis": legal_basis,
            "completed_at": verified_at.isoformat(), "attestation": attestation}


def verify_attestation(attestation: dict, key: str) -> bool:
    """Recompute the HMAC over the attestation payload; True if intact."""
    payload = {k: v for k, v in attestation.items() if k != "signature"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, attestation.get("signature", ""))
