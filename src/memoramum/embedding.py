"""Pluggable embedding providers.

Retrieval is hybrid (doc 04 §3) with the lexical leg always on; the vector
leg activates when an embedder is configured. P1 ships no external model
dependency: 'none' disables the vector leg, 'hash' is a deterministic
local bag-of-words embedder good enough for dev and tests. A real model
provider slots in behind the same one-method interface.

Embeddings are computed AFTER redaction once the PII pipeline exists
(doc 06 §4); in P1 the content is embedded as written.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

DIM = 1536  # matches vector(1536) in the doc 02 DDL


class Embedder(Protocol):
    def embed(self, text: str) -> list[float] | None: ...


class NoneEmbedder:
    def embed(self, text: str) -> None:
        return None


class HashEmbedder:
    """Deterministic sparse-ish bag-of-words vector: each token hashes to a
    handful of dimensions. No semantics — overlap-based similarity only."""

    def embed(self, text: str) -> list[float] | None:
        vec = [0.0] * DIM
        tokens = re.findall(r"[a-z0-9]+", text.lower())
        if not tokens:
            return None
        for tok in tokens:
            digest = hashlib.sha256(tok.encode()).digest()
            for i in range(0, 12, 4):
                idx = int.from_bytes(digest[i : i + 3], "big") % DIM
                sign = 1.0 if digest[i + 3] % 2 else -1.0
                vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def make_embedder(name: str) -> Embedder:
    if name == "none":
        return NoneEmbedder()
    if name == "hash":
        return HashEmbedder()
    raise ValueError(f"unknown embedder {name!r} (expected 'none' or 'hash')")
