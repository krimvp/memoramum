-- 0004_policy: the P3 policy & promotion surface (doc 07 §6).
--
-- Three tables, none of them part of the doc 02 core model:
--
--   policies        versioned learning/access policy documents (doc 05 §5:
--                   "YAML in, canonical JSON out"); the latest version per
--                   (layer, applies_to) is the active one. Policy *changes*
--                   are POLICY_CHANGE events in memory_events — this table
--                   is the document store, the event log stays the record.
--   pending_actions the candidate store behind `ask` verdicts (doc 04 §1:
--                   "the confirmation comes back via memory_confirm") and
--                   promotion confirmations (doc 05 §3). Operational queue
--                   state like contradiction_queue: a lost pending re-asks,
--                   it never corrupts memories or events.
--   pii_tokens      the stable per-scope pseudonym map of the PII pipeline
--                   (doc 06 §4: "TOKENIZE (<PERSON_7>, stable per scope,
--                   reversible under privilege)").

CREATE TABLE policies (
    name        text NOT NULL,
    layer       text NOT NULL CHECK (layer IN ('org','surface','agent','user_pref')),
    applies_to  text NOT NULL,             -- 'org:acme' | 'surface:slack' | 'agent:sage' | 'user:dana'
    version     integer NOT NULL,          -- monotonic per (layer, applies_to)
    document    jsonb NOT NULL,            -- canonical JSON form of the policy document
    created_by  text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (layer, applies_to, version)
);

CREATE TABLE pending_actions (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    action             text NOT NULL CHECK (action IN ('remember','promote')),
    payload            jsonb NOT NULL,     -- the full candidate, enacted verbatim on approval
    requested_by       text NOT NULL,      -- the principal whose call earned the ask
    on_behalf_of       text,
    source_scope_id    text REFERENCES scopes(id),
    target_scope_id    text NOT NULL REFERENCES scopes(id),
    confirmer          text NOT NULL CHECK (confirmer IN ('flow_user','scope_member','subject')),
    ask_prompt         text NOT NULL,      -- relayed verbatim to the human (doc 04 §4)
    policy_decision_id uuid,               -- the POLICY_DECISION event that returned `ask`
    rule_id            text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    resolved_at        timestamptz,
    approved           boolean,
    resolved_by        text,               -- the confirming/declining human
    note               text
);
CREATE INDEX ON pending_actions (created_at) WHERE resolved_at IS NULL;
CREATE INDEX ON pending_actions (on_behalf_of) WHERE resolved_at IS NULL;

CREATE TABLE pii_tokens (
    scope_id    text NOT NULL REFERENCES scopes(id),
    entity_kind text NOT NULL,             -- 'person', 'email', …
    value       text NOT NULL,             -- the original surface form (privileged view)
    token       text NOT NULL,             -- '<PERSON_7>'
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, entity_kind, value),
    UNIQUE (scope_id, token)
);
