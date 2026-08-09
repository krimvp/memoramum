# 02 — Data model

Reference schema for the memory store. Given in Postgres terms (the reference stack, [doc 07 §3](07-operations.md)), but every access goes through the service API — the DDL is a specification of *shape*, not a public contract. Storage-agnostic interfaces sit above it; [ADR-0004](adr/0004-postgres-reference-stack.md) records why one Postgres.

Four core tables: `scopes`, `episodes`, `memories` (+ `memory_provenance`), `memory_events`. Policy tables live in [doc 05](05-policy.md).

---

## 1. Scopes

```sql
CREATE TABLE scopes (
    id              text PRIMARY KEY,          -- e.g. 'channel/C0DEP', 'subject:user/dana'
    family          text NOT NULL CHECK (family IN ('container','subject','agent','org','surface','module')),
    parent_scope_id text REFERENCES scopes(id),-- tree lives here, not in id parsing
    surface         text,                      -- 'slack', 'gitlab', NULL for cross-surface families
    external_ref    jsonb,                     -- surface-native ids: {"team":"T024B","channel":"C0DEP"}
    trust_class     text NOT NULL DEFAULT 'internal_public'
                    CHECK (trust_class IN ('private','internal_public','shared_external')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
```

- `trust_class` encodes the isolation ordering of [doc 01 §3.2](01-concepts-and-scopes.md): policy defaults key off it (nothing auto-promotes out of `private`; `shared_external` scopes get stricter write policy).
- Scope *membership* (which users are in a channel) is **not** stored here — it is resolved live against the surface or a synced relation table ([doc 05 §4](05-policy.md)), because the source-visibility invariant requires membership checks at retrieval time, not at index time.
- The `module` family holds monorepo-module conventions ([doc 01 §3.1](01-concepts-and-scopes.md), [ADR-0009](adr/0009-module-scope-family.md)): `parent_scope_id` is the project container, so access review stays a subtree walk. Which modules a flow touches is resolved from file paths through an operational **`module_paths`** table (`module_scope_id`, `glob`; most-specific match wins), admin-maintained and consulted at both write-time tagging and read-time chain inclusion ([ADR-0010](adr/0010-module-boundary-detection.md)). Unmatched paths belong to no module and fall back to the project scope.

## 2. Memories

```sql
CREATE TABLE memories (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind             text NOT NULL CHECK (kind IN ('semantic','episodic','procedural','profile')),
    content          text NOT NULL,             -- canonical natural-language statement
    content_embedding vector(1536),             -- embedded AFTER redaction (doc 06 §4)
    content_tsv      tsvector GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,

    scope_id         text NOT NULL REFERENCES scopes(id),
    subject_ids      text[] NOT NULL DEFAULT '{}',   -- principals this memory is ABOUT (GDPR index)
    categories       text[] NOT NULL DEFAULT '{}',   -- policy vocabulary: 'preference','process','health',…
    sensitivity      text NOT NULL DEFAULT 'internal'
                     CHECK (sensitivity IN ('public','internal','confidential','restricted')),

    status           text NOT NULL DEFAULT 'staged'
                     CHECK (status IN ('staged','active','invariant','deprecated','archived','tombstoned')),

    -- belief & security signals
    confidence       real NOT NULL DEFAULT 0.7,      -- how sure are we the statement is true (0..1)
    trust_score      real NOT NULL DEFAULT 0.5,      -- provenance-derived (doc 06 §3), floor-filterable

    -- decay inputs (doc 03 §5)
    strength         real NOT NULL DEFAULT 1.0,      -- S in R = exp(-t/S); reinforcement increments
    last_accessed_at timestamptz NOT NULL DEFAULT now(),
    access_count     integer NOT NULL DEFAULT 0,

    -- bi-temporal validity (doc 03 §3)
    valid_at         timestamptz,                    -- when the fact became true in the world
    invalid_at       timestamptz,                    -- when it stopped being true (NULL = still true)
    recorded_at      timestamptz NOT NULL DEFAULT now(),  -- when the system learned it
    superseded_by    uuid REFERENCES memories(id),   -- successor, set on supersession
    expires_at       timestamptz                     -- policy TTL; NULL = no TTL
);

CREATE INDEX ON memories USING hnsw (content_embedding vector_cosine_ops);
CREATE INDEX ON memories USING gin (content_tsv);
CREATE INDEX ON memories USING gin (content gin_trgm_ops);   -- literal leg (ADR-0017)
CREATE INDEX ON memories (scope_id, status);
CREATE INDEX ON memories USING gin (subject_ids);
```

Notes:

- **`confidence` vs `trust_score`**: confidence is epistemic ("how sure is the statement true" — set at extraction, adjusted by reinforcement/contradiction); trust is *security* ("how much do we trust the source" — derived from provenance, used as a retrieval floor and the poisoning lever). They move independently: a confidently-extracted fact from an external Slack Connect user is high-confidence, low-trust.
- **`subject_ids` at write time** is non-negotiable: honoring "what do you know about dana" or a GDPR erasure request must be an index lookup, not a semantic search ([doc 06 §2](06-audit-privacy-security.md)).
- **`categories`** is the shared vocabulary between memories and the learning policy ([doc 05 §2](05-policy.md)) — policy rules match on it (`deny: credentials`, `ask: health`).
- **Three retrieval indexes, three legs.** `content_embedding` (HNSW) carries paraphrase, `content_tsv` (GIN) carries stemmed natural language, and the trigram index over raw `content` carries literal tokens — paths, symbol names, workspace and MR ids — that the `english` parser folds into one lexeme and an embedder has no signal for ([ADR-0017](adr/0017-literal-retrieval-leg.md)). `pg_trgm` is therefore a required extension alongside `vector`; the fusion and the per-leg admission gates are [doc 04 §3](04-agent-interface.md).
- Content is immutable (enforced in the service layer; `profile` kind excepted, every edit evented). Everything below the "belief & security" divider is mutable bookkeeping.

## 3. Episodes

Episodes are the **provenance atoms**: verbatim pointers to (and, where retention policy allows, copies of) the raw sources memories were derived from. The Graphiti pattern: keep the non-lossy source; let every derived fact point back.

```sql
CREATE TABLE episodes (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id     text NOT NULL REFERENCES scopes(id),
    source_kind  text NOT NULL,          -- 'slack_message','mr_comment','api_call','user_directive',…
    external_ref jsonb NOT NULL,         -- {"channel":"C0DEP","ts":"1709481600.123"} — enough to deep-link
    content      text,                   -- verbatim excerpt; NULLable (see retention note)
    author       text,                   -- principal id of who produced the source, e.g. 'user:dana'
    occurred_at  timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
```

Retention note: storing verbatim `content` duplicates data the surface already holds and inherits its retention obligations (a Slack message deleted upstream should not live on here). Default: store the `external_ref` always, the verbatim excerpt only when policy opts in (e.g. surfaces whose history is volatile), and subscribe to upstream deletions to null out copies ([doc 06 §2](06-audit-privacy-security.md)).

## 4. Provenance

PROV-shaped (entity ← activity ← agent), one row per memory, plus a derivation edge table. This is the structure that simultaneously serves audit ("where did this come from, who, why, explicit or suggested?"), security (trust scoring, lineage quarantine), and GDPR (erasure cascade).

```sql
CREATE TABLE memory_provenance (
    memory_id        uuid PRIMARY KEY REFERENCES memories(id),
    origin_kind      text NOT NULL CHECK (origin_kind IN
                       ('explicit_user_ask',  -- user said "remember X"
                        'llm_inferred',       -- agent proposed it mid-conversation (hot path)
                        'agent_observed',     -- background extraction over episodes
                        'consolidated',       -- produced by the consolidator from other memories
                        'imported')),         -- bulk/manual import
    responsible_agent text NOT NULL,          -- 'agent:sage' | 'user:dana' | 'system:consolidator'
    on_behalf_of      text,                   -- user id when an agent acted for someone
    activity          jsonb NOT NULL,         -- {"session":"…","model":"…","prompt_version":"…","policy_decision_id":"…"}
    justification     text NOT NULL,          -- LLM- or rule-emitted "why this was kept"
    extraction_confidence real
);

CREATE TABLE memory_derivations (              -- PROV wasDerivedFrom, N per memory
    memory_id   uuid NOT NULL REFERENCES memories(id),
    source_type text NOT NULL CHECK (source_type IN ('episode','memory')),
    source_id   uuid NOT NULL,
    PRIMARY KEY (memory_id, source_type, source_id)
);
CREATE INDEX ON memory_derivations (source_type, source_id);   -- reverse walk: erasure cascade, quarantine
```

The reverse index is the workhorse: *erasure cascade* ("delete everything derived from this message") and *poisoning quarantine* ("suspend everything derived from source S") are both reverse walks over `memory_derivations`.

`origin_kind` directly answers the audit requirement "was this an explicit ask or suggested by the LLM," and it drives lifecycle defaults: `explicit_user_ask` skips staging; `llm_inferred`/`agent_observed` start `staged` ([doc 03 §2](03-lifecycle.md)); `consolidated` inherits the *minimum* trust of its inputs.

## 5. The event log

Append-only, INSERT-only-role, the single source of truth for *what happened*. The `memories` table is, conceptually, a projection of this log plus mutable bookkeeping.

```sql
CREATE TABLE memory_events (
    seq          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id     uuid NOT NULL DEFAULT gen_random_uuid(),
    at           timestamptz NOT NULL DEFAULT now(),
    actor        text NOT NULL,            -- principal id
    on_behalf_of text,
    action       text NOT NULL CHECK (action IN
                   ('PROPOSE','CONFIRM','REJECT','PROMOTE_STATUS','PROMOTE_SCOPE',
                    'REINFORCE','SUPERSEDE','DEPRECATE','ARCHIVE','RESTORE',
                    'FORGET','TOMBSTONE','QUARANTINE',
                    'READ',                 -- memory delivered into an agent context
                    'POLICY_DECISION',      -- allow/stage/ask/deny outcome, with rule id
                    'POLICY_CHANGE','ENROLLMENT_CHANGE')),
    memory_id    uuid,                     -- NULLable: policy/enrollment events aren't per-memory
    scope_id     text,
    details      jsonb NOT NULL DEFAULT '{}',  -- action-specific: query, rule_id, old/new scope, context block id…
    prev_hash    bytea,                    -- optional hash chain: sha256(canonical(event) || prev_hash)
    curr_hash    bytea
);
CREATE INDEX ON memory_events (memory_id, at);
CREATE INDEX ON memory_events (actor, at);
CREATE INDEX ON memory_events (action, at);
```

Design points:

- **Reads are events.** Every memory delivered into an agent's context — ambient block or deliberate search — produces `READ` events (batched: one event per delivery with the memory-id list in `details`). This is the single most load-bearing audit decision: it is what answers "why did the bot say that?" and it is what makes reinforcement-by-useful-retrieval measurable. Cost trade-offs and mitigation in [ADR-0006](adr/0006-log-reads.md).
- **Policy decisions are events**, including denials. "What has agent X been prevented from learning" is a query, not a shrug.
- **Erasure-compatible.** Events carry ids and metadata, not memory content (content lives in `memories`/`episodes`); after a hard erasure the log keeps a content-free `TOMBSTONE` event and the chain stays intact — hashes commit to event envelopes, not to erased content ([doc 06 §2](06-audit-privacy-security.md)).
- **Hash chain optional.** On by default per-scope-family for `subject:*` scopes (highest compliance value), off elsewhere until needed; periodic head-hash anchoring to an object-lock bucket. Tamper-*evidence*, not tamper-*proofing*.

## 6. Retention & sizing posture

| Table | Growth driver | Posture |
|---|---|---|
| `memories` | distilled knowledge | small by design (atomic facts, aggressive consolidation); archived rows move to `status=archived`, cold storage export after N months |
| `episodes` | source traffic | bounded by opt-in verbatim retention; refs are cheap |
| `memory_events` | reads dominate | ≥15-month online retention (SOC 2 posture), partitioned by month, cold-archived after |

Continue with [doc 03 — Lifecycle](03-lifecycle.md): how a row in `memories` moves through `status`, and what the temporal fields mean operationally.
