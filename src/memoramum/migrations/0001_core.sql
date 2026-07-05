-- 0001_core: the four core tables, verbatim from docs/02-data-model.md.
-- The DDL there is the specification of shape; this file is its executable form.

CREATE EXTENSION IF NOT EXISTS vector;

-- doc 02 §1 — Scopes
CREATE TABLE scopes (
    id              text PRIMARY KEY,          -- e.g. 'channel/C0DEP', 'subject:user/dana'
    family          text NOT NULL CHECK (family IN ('container','subject','agent','org','surface')),
    parent_scope_id text REFERENCES scopes(id),-- tree lives here, not in id parsing
    surface         text,                      -- 'slack', 'gitlab', NULL for cross-surface families
    external_ref    jsonb,                     -- surface-native ids: {"team":"T024B","channel":"C0DEP"}
    trust_class     text NOT NULL DEFAULT 'internal_public'
                    CHECK (trust_class IN ('private','internal_public','shared_external')),
    created_at      timestamptz NOT NULL DEFAULT now()
);

-- doc 02 §2 — Memories
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
CREATE INDEX ON memories (scope_id, status);
CREATE INDEX ON memories USING gin (subject_ids);

-- doc 02 §3 — Episodes
CREATE TABLE episodes (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scope_id     text NOT NULL REFERENCES scopes(id),
    source_kind  text NOT NULL,          -- 'slack_message','mr_comment','api_call','user_directive',…
    external_ref jsonb NOT NULL,         -- {"channel":"C0DEP","ts":"1709481600.123"} — enough to deep-link
    content      text,                   -- verbatim excerpt; NULLable (retention note, doc 02 §3)
    author       text,                   -- principal id of who produced the source, e.g. 'user:dana'
    occurred_at  timestamptz NOT NULL,
    recorded_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON episodes (scope_id, occurred_at);

-- doc 02 §4 — Provenance
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

-- doc 02 §5 — The event log
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
