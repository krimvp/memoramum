-- 0006_membership_sync: the doc 07 §5 staleness bound's watermark.
--
-- Ingestion (doc 07 §1) syncs surface membership into scope_relations
-- and heartbeats this table after every completed sync cycle. Retrieval
-- compares the watermark against the staleness bound (default 5 min):
-- beyond it, private-trust-class surface scopes fail closed, everything
-- else serves with the staleness noted in the READ event.
--
-- A surface with no row here has never synced — its membership is
-- authored directly in the service (set_relation) and has no sync to go
-- stale. That makes seeding the row part of *enabling* sync for a
-- surface, and means a deleted row silently disables the bound: this is
-- an operational table an admin owns, not queue state a lost row merely
-- degrades.

CREATE TABLE membership_sync (
    surface   text PRIMARY KEY,               -- 'slack', 'gitlab'
    synced_at timestamptz NOT NULL DEFAULT now()
);
