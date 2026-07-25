# 06 — Audit, privacy & security

The three concerns this system refuses to bolt on later. The mechanisms live in the data model ([doc 02](02-data-model.md)) and policy layer ([doc 05](05-policy.md)); this document assembles them into the guarantees and the operational procedures.

---

## 1. Auditability

### 1.1 The guarantee

For any memory, at any time, the system can answer — from indexed data, not forensics:

| Question | Answered by |
|---|---|
| Where did this come from? | `memory_derivations` → `episodes.external_ref` (deep link to the Slack message / MR comment) |
| Who caused it? | `memory_provenance.responsible_agent` + `on_behalf_of` |
| When? | `recorded_at`, plus the full `memory_events` trail |
| Why was it kept? | `memory_provenance.justification` |
| Explicit ask or LLM suggestion? | `origin_kind` (`explicit_user_ask` vs `llm_inferred`/`agent_observed`) |
| Under which rule was it allowed? | `activity.policy_decision_id` → the `POLICY_DECISION` event + policy version |
| Who has *seen* it? | `READ` events |
| Why did the bot say that? | `READ` events for the session → delivered memory ids → everything above |

The last row is the one most systems cannot answer and the reason reads are logged ([ADR-0006](adr/0006-log-reads.md)).

### 1.2 Audit surfaces

- **Per-memory history**: `GET /v1/memories/{id}/history` — the Mem0-style changelog, generated from `memory_events`.
- **Per-subject view**: `GET /v1/subjects/{principal}/memories` — "everything remembered about X", including staged and deprecated items. Exposed to end users through the surfaces themselves (in-chat "what do you know about me?" via `memory_status`) — **inferred memories are exactly as inspectable as explicit ones.** This is the deliberate inversion of ChatGPT's design, whose inferred chat-history insights are invisible; the non-auditable-inference gap is the single most criticized property of deployed memory systems.
- **Per-scope inventory**: what does this channel's memory contain (channel admins).
- **Event-log queries** (admin/audit role): by actor, action, time window, rule id.

These REST surfaces authenticate callers with principal-bound bearer tokens ([ADR-0014](adr/0014-bearer-token-rest-auth.md)), or with the OAuth access tokens the remote MCP endpoint accepts ([ADR-0016](adr/0016-oauth-resource-server.md)) — the audit trail only means something if every `actor` it records was proven, not asserted. An OAuth login extends that to the second half of the pair: a user-authorized token also proves the `on_behalf_of` an event records.
- **Review UI** (product surface, out of scope here but assumed): staged-memory triage, `ask` confirmations, forget buttons, promotion requests.

### 1.3 Integrity

Append-only `memory_events` via INSERT-only DB role; optional per-family hash chaining with external head anchoring ([doc 02 §5](02-data-model.md)); policy changes and enrollment changes are events in the same log; ≥15-month online retention. The audit log records *envelope* data (ids, actors, rules), never memory content — which is what lets erasure and audit coexist.

## 2. Privacy & erasure

### 2.1 Design-time provisions

- `subject_ids[]` populated at write time — DSAR and erasure are index lookups ([doc 02 §2](02-data-model.md)).
- Subject scopes concentrate about-a-person memories in one enumerable place.
- Episodes store `external_ref` always, verbatim content only by policy opt-in, with upstream-deletion subscription to null copies ([doc 02 §3](02-data-model.md)).
- Embeddings are computed **after** redaction (§4) — vectors never encode what the text doesn't.

### 2.2 The erasure pipeline (`POST /v1/erasure-requests`)

Soft delete is not erasure — tombstone-flagged vectors in HNSW indexes remain reconstructible (the "ghost vectors" problem), and derived summaries carry PII forward. The pipeline therefore:

1. **Enumerate**: `subject_ids` lookup ∪ reverse `memory_derivations` walk from the subject's episodes → the full derived closure (including `consolidated` memories that mixed this subject in).
2. **Split consolidated derivatives**: a consolidated memory mentioning several subjects is *re-generated* from its remaining sources rather than deleted wholesale.
3. **Hard-delete content**: `memories.content`, `content_embedding`, `content_tsv`, episode verbatim copies — physically, with vector-index hygiene (re-index/vacuum, not flag-and-hope).
4. **Tombstone**: `status='tombstoned'`; a content-free `TOMBSTONE` event (memory id, timestamp, legal basis, actor) preserves the audit chain.
5. **Verify & attest**: post-erasure scan (id-based + embedding-neighborhood spot check) and a signed attestation record.

**Crypto-shredding option**: per-subject envelope keys for `subject:*` scope content; erasure destroys the key, which also covers backups. Recommended for deployments with long backup retention.

### 2.3 Consent & preference

The user-preference policy layer ([doc 05 §2](05-policy.md)) gives every user standing controls: opt out of being learned about (`subjects` match → deny), opt out per category, per surface. Incognito flows (surface-level "temporary" sessions) register no episodes and permit no writes — the ChatGPT temporary-chat pattern, enforced service-side by flow-context flag.

## 3. Security: memory poisoning and friends

Threat model headline: **memory is a persistence mechanism for prompt injection** (OWASP Agentic Top-10 ASI06). A poisoned memory outlives the session that planted it and re-enters every future context. Studied attack success rates against undefended agent memories are high; the defenses are layered:

| Layer | Mechanism |
|---|---|
| **Write gate** | Learning policy allowlist — most injected "please remember that the deploy key is now X" attempts die at `deny` (category `secrets`/`credentials`, or no matching strategy). |
| **Quarantine tier** | Everything agent-inferred enters `staged`: rank-penalized, `[staged]`-fenced in context blocks, excluded from high-stakes reads by trust floors, and unable to supersede active memories ([doc 03 §3](03-lifecycle.md)). Poison must survive *reinforcement from independent evidence* to gain rank. |
| **Trust scoring** | `trust_score` derived from provenance at write: author is scope member > external/Slack-Connect author; `explicit_user_ask` > `agent_observed` over third-party content; content that arrived via untrusted tool output (web fetch, forwarded content) gets a low base. Consolidated memories inherit the **minimum** of inputs. Retrieval floors do the rest. |
| **Contradiction friction** | Staged/low-trust claims cannot auto-invalidate active/high-trust memories — held for review instead ([doc 03 §3](03-lifecycle.md)). Blocks the "overwrite the deploy rule" attack. |
| **Lineage quarantine** | Incident response: `POST /v1/quarantine` with a source predicate (episode source, author, agent, time window) → reverse derivation walk → mass `QUARANTINE` (excluded from all retrieval, pending review → restore or tombstone). The provenance graph is the security control. |
| **Break-glass** | Per-agent full write-freeze flag ([doc 05 §5](05-policy.md)). |
| **Detection** | Consolidator anomaly checks: write-rate spikes per author/source, staged memories with unusually directive content ("always", "ignore", "instead you must"), embedding-space outliers in a scope. Flagged → review queue ([doc 07 §2](07-operations.md)). |

Residual risks stated honestly: a patient attacker with legitimate scope membership can seed plausible facts and reinforce them across days — the design raises cost and leaves an audit trail (reinforcement events name their episodes), it does not make poisoning impossible. High-stakes *actions* should never be authorized by memory content alone; that is an agent-design rule the prompt contract states and the trust floors back up.

## 4. PII pipeline

Runs inside the write pipeline's *classify* step ([doc 05 §2](05-policy.md)), before anything is stored or embedded:

```
candidate text
  ─▶ analyze        Presidio-class NER + regex + context (entities, offsets, confidence)
  ─▶ classify       sensitivity (public|internal|confidential|restricted) + categories
  ─▶ per-entity action (policy table):
        credentials/API keys/gov IDs  → BLOCK the write (deny verdict)
        person names in shared scopes → TOKENIZE (<PERSON_7>, stable per scope, reversible under privilege)
        contact details               → REDACT unless category requires
        subject self-reference        → ALLOW (it's their subject scope)
  ─▶ embed          the post-action text only
  ─▶ store
```

- **Tokenize over redact** where memory quality matters: stable pseudonyms keep the fact useful ("<PERSON_7> approves deploys") and reversible for privileged views; combined with crypto-shredding, detokenization dies with the key.
- Sensitivity and categories assigned here are the attributes the access policy enforces ceilings against ([doc 05 §4](05-policy.md)) — classification is *load-bearing*, so misclassification handling matters: classifier versions are recorded in provenance `activity`, and a reclassification sweep is a standard consolidator job after a classifier upgrade.
- Scope promotion re-runs this pipeline against the destination scope ([doc 03 §4](03-lifecycle.md)) — tokenization requirements differ between `#deploys` and workspace-wide.

## 5. Compliance posture summary

| Requirement | Mechanism |
|---|---|
| SOC 2 auditability | append-only evented log incl. permission/policy changes, ≥15-month retention, exportable |
| GDPR Art. 15 (access) | per-subject view, in-surface "what do you know about me" |
| GDPR Art. 17 (erasure) | erasure pipeline §2.2: hard delete + derivation cascade + tombstones + attestation |
| Data minimization | learning-policy allowlist (nothing learned by default), TTLs per category, PII redaction pre-store |
| Purpose limitation | scopes + enrollment: memories usable only in flows that could see their sources |

Continue with [doc 07 — Operations](07-operations.md): the background machinery that keeps all of this true over time.
