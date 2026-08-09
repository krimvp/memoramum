# ADR-0017 — Give retrieval a third, literal leg

**Status:** accepted · **Context docs:** [04 §3](../04-agent-interface.md), [02 §2](../02-data-model.md), [07 §3](../07-operations.md)

## Decision

Retrieval fuses **three** legs, not two ([doc 04 §3](../04-agent-interface.md)):

| Leg | Index | Finds |
|---|---|---|
| lexical | `gin (content_tsv)`, `to_tsvector('english', …)` | stemmed natural language — "deploys" for "deploy" |
| vector | `hnsw (content_embedding vector_cosine_ops)` | paraphrase and synonymy |
| **literal** | `gin (content gin_trgm_ops)`, `pg_trgm` word similarity | `payments/ledger.py`, `PaymentLedger`, `T024B`, `!482` |

The literal leg matches the query against its best-matching *word extent* in the content (`query <% content`, ranked by `word_similarity`), above a threshold. `pg_trgm` joins `vector` as a required extension of the reference stack ([doc 02 §2](../02-data-model.md)).

## Alternatives considered

1. **A second `tsvector` column with the `simple` configuration** — no stemming, no stopword removal. Cheaper, and the obvious first reach.
2. **An `identifiers text[]` column** populated at write time by a tokenizer that splits camelCase, paths, and issue refs, matched exactly with a GIN index.
3. **Nothing** — rely on the vector leg and a better embedding model.

## Why trigrams win here

1. **`simple` changes the dictionary, not the parser.** Both configurations run the same tokenizer: `PaymentLedger` is one lexeme either way, `payments/ledger.py` is one `file` token either way. Turning off stemming does not buy substring reach, which is the entire problem — a developer asking about `ledger` never matches content that says `LedgerEntry`.
2. **The write path shouldn't have to know every naming convention.** An `identifiers` column is precise but is a *commitment*: a tokenizer that must learn camelCase, snake_case, kebab, paths, `!MR`/`#issue` refs, and be re-run over the whole corpus each time it learns one. It also cannot do partial matching, which is most of what a human types. Trigrams need no write-time schema decision and no re-extraction.
3. **Doc 04 §3 already made this argument; P5 sharpened it.** "Exact tokens (team names, service names, MR numbers) are where pure-vector recall fails" was true when the surfaces were Slack and GitLab. With the dev-time surface and module scopes ([ADR-0012](0012-dev-time-agent-surface.md), [ADR-0013](0013-dev-time-routing-defaults.md)) the memories are codebase conventions — statements *about* paths and symbols — and an embedder has close to no signal for an identifier it has never seen.
4. **It degrades to silence, not to noise.** `word_similarity` scores the whole query against its best extent in the content, so a twelve-word question almost never clears the threshold while a bare identifier clears it easily. The leg contributes candidates exactly where the other two are weak and contributes nothing where they are strong — which is what makes adding it safe under RRF, where a third leg costs one more rank list and no new score scale.

## Costs accepted

A GIN trigram index over `content` is the largest index on `memories` and trigram indexes are write-amplifying — the write path pays for a read-path win, and the [doc 07 §3](../07-operations.md) revisit trigger for outgrowing pgvector now has a second index to carry. The reference stack requires two extensions rather than one. Word similarity is script-blind: it matches character shape, so a query can trigram-match content in another language that merely looks alike; the absolute gate of [ADR-0016](0016-absolute-relevance-admission.md) bounds how much of that reaches a block, and the threshold is one more per-deployment knob. Finally, three legs make an RRF tie between a strong single-leg hit and a weak two-leg hit slightly more likely — accepted, because the fusion's insensitivity to per-leg score scales is exactly why it was chosen.
