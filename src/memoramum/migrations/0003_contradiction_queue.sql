-- 0003_contradiction_queue: the held-contradiction queue (doc 03 §3,
-- worked by the consolidator per doc 07 §2). Operational job-queue state
-- (doc 07 §1/§3: "any boring queue", here Postgres-based), not part of the
-- doc 02 core model — memories rows and memory_events stay the source of
-- truth; a wedged queue degrades review latency, never correctness.

CREATE TABLE contradiction_queue (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contradicted_id uuid NOT NULL REFERENCES memories(id),
    challenger_id   uuid REFERENCES memories(id),  -- NULL: reinforce(signal='wrong'), no successor claim
    queued_by       text NOT NULL,                 -- principal whose write/signal queued it
    queued_at       timestamptz NOT NULL DEFAULT now(),
    escalated_at    timestamptz,                   -- consolidator: escalate to review after 7 days
    resolved_at     timestamptz,
    resolution      text CHECK (resolution IN ('supersede','keep_both','reject','archived')),
    note            text
);
CREATE INDEX ON contradiction_queue (queued_at) WHERE resolved_at IS NULL;
CREATE INDEX ON contradiction_queue (contradicted_id);
CREATE INDEX ON contradiction_queue (challenger_id);
