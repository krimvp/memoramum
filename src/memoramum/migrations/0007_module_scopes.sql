-- 0007_module_scopes: the P5 module-scope surface (doc 07 §6; ADR-0009/0010/0011).
--
-- Module scopes are a parallel scope family (like subject/agent, ADR-0009),
-- homed under a project container so access review stays a subtree walk and
-- project-level enrollment governs them. Boundaries come from an explicit,
-- admin-maintained path-glob mapping — module_paths (ADR-0010) — not from
-- CODEOWNERS or directory convention. mr→module and module→project
-- promotions confirm against explicit relation tuples: module_owner on the
-- module scope, maintainer on the project scope (ADR-0011).
--
--   module_paths  the ADR-0010 mapping: repository path globs → module
--                 scope. Longest/most-specific glob wins per path (resolved
--                 in scopes.modules_touched). An operational table an admin
--                 owns: a stale glob quietly routes new paths to the project
--                 scope (visible in staged-triage, not a correctness fault),
--                 never to a wrong module.

-- doc 02 §1 — module joins the scope families.
ALTER TABLE scopes DROP CONSTRAINT scopes_family_check;
ALTER TABLE scopes ADD CONSTRAINT scopes_family_check
    CHECK (family IN ('container','subject','agent','org','surface','module'));

-- doc 05 §3 / ADR-0011 — module_owner (module scope) and maintainer
-- (project scope) join the relation vocabulary as promotion confirmers.
ALTER TABLE scope_relations DROP CONSTRAINT scope_relations_relation_check;
ALTER TABLE scope_relations ADD CONSTRAINT scope_relations_relation_check
    CHECK (relation IN ('member','owner','reader_agent','writer_agent','auditor',
                        'module_owner','maintainer'));

-- pending_actions grows the two ReBAC confirmers those crossings resolve
-- against (doc 04 §1: "the confirmation comes back via memory_confirm").
ALTER TABLE pending_actions DROP CONSTRAINT pending_actions_confirmer_check;
ALTER TABLE pending_actions ADD CONSTRAINT pending_actions_confirmer_check
    CHECK (confirmer IN ('flow_user','scope_member','subject','module_owner','maintainer'));

CREATE TABLE module_paths (
    module_scope_id text NOT NULL REFERENCES scopes(id),
    glob            text NOT NULL,             -- fnmatch glob over repository paths
    PRIMARY KEY (module_scope_id, glob)
);
CREATE INDEX ON module_paths (module_scope_id);
