-- 0005_background_and_forgetting: the P4 surface (doc 07 §6).
--
-- Operational tables only — the doc 02 core model already carries every
-- field these jobs populate ("nothing later requires reworking earlier
-- data"). Like contradiction_queue and pending_actions, a lost row here
-- degrades a background job or a review queue, never memories or events.
--
--   extraction_state    per-scope watermark for the extraction workers
--                       (doc 07 §1: debounced background reflection —
--                       accumulate, run at conversation-lull)
--   quarantine_requests the lineage-quarantine incident lever (doc 06 §3:
--   quarantine_items    source predicate → reverse derivation walk →
--                       mass QUARANTINE, pending review → restore or
--                       tombstone). Items, not a memory status: statuses
--                       are the doc 02 enum; exclusion-from-retrieval is
--                       an unresolved item joined at read time.
--   agent_freezes       break-glass per-agent write-freeze (doc 05 §5,
--                       doc 06 §3): one flag, evented, writes deny.
--   erasure_requests    the GDPR pipeline record (doc 06 §2.2): what was
--                       enumerated, when it completed, and the signed
--                       attestation of step 5.

CREATE TABLE extraction_state (
    scope_id          text PRIMARY KEY REFERENCES scopes(id),
    processed_through timestamptz NOT NULL,   -- episodes.recorded_at watermark
    last_run_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quarantine_requests (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    predicate    jsonb NOT NULL,              -- {episode_id | author | source_kind | agent | scope_id | occurred_from/to}
    requested_by text NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE quarantine_items (
    request_id     uuid NOT NULL REFERENCES quarantine_requests(id),
    memory_id      uuid NOT NULL REFERENCES memories(id),
    quarantined_at timestamptz NOT NULL DEFAULT now(),
    resolved_at    timestamptz,
    resolution     text CHECK (resolution IN ('restore','tombstone')),
    resolved_by    text,
    note           text,
    PRIMARY KEY (request_id, memory_id)
);
CREATE INDEX ON quarantine_items (memory_id) WHERE resolved_at IS NULL;

CREATE TABLE agent_freezes (
    agent       text NOT NULL,
    frozen_at   timestamptz NOT NULL DEFAULT now(),
    frozen_by   text NOT NULL,
    reason      text,
    released_at timestamptz,
    released_by text
);
CREATE INDEX ON agent_freezes (agent) WHERE released_at IS NULL;

CREATE TABLE erasure_requests (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    subject      text NOT NULL,
    legal_basis  text NOT NULL DEFAULT 'gdpr_art_17',
    requested_by text NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    attestation  jsonb                        -- doc 06 §2.2 step 5, HMAC-signed
);

-- memory_forget (doc 04 §1) routes to `ask` outside an agent's own
-- authority: pending_actions grows the third action.
ALTER TABLE pending_actions DROP CONSTRAINT pending_actions_action_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_action_check
    CHECK (action IN ('remember','promote','forget'));
