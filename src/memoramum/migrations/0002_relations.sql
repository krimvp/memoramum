-- 0002_relations: scope membership / enrollment tuples, doc 05 §4.1.
-- Zanzibar-style relation tuples kept SpiceDB/OpenFGA-isomorphic (ADR-0004).

CREATE TABLE scope_relations (
    scope_id   text NOT NULL REFERENCES scopes(id),
    relation   text NOT NULL CHECK (relation IN ('member','owner','reader_agent','writer_agent','auditor')),
    principal  text NOT NULL,             -- 'user:dana', 'agent:sage'
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_id, relation, principal)
);
CREATE INDEX ON scope_relations (principal, relation);
