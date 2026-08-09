-- 0008_literal_leg: the third retrieval leg (doc 04 §3; ADR-0017).
--
-- The lexical leg runs the `english` text-search configuration, whose parser
-- makes `payments/ledger.py` one `file` token and `PaymentLedger` one lexeme.
-- A developer asking about `ledger` matches neither, and an embedder has no
-- signal for an identifier it has never seen. A trigram index over the raw
-- content gives the third leg substring reach with no write-time schema
-- commitment; retrieval matches with pg_trgm's word-similarity operator
-- (`query <% content`), which scores the query against its best-matching
-- word extent rather than against the whole text.
--
-- The index is the doc 02 §2 DDL line; the extension is what makes it legal.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX ON memories USING gin (content gin_trgm_ops);
