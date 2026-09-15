"""
Thread Memory V1 — embedding abstraction.

VERONICA does NOT hard-code one embedding model into Thread. Higher layers depend on
``EmbeddingProvider``; which concrete provider is used is decided by configuration.

V1 ships one provider:

    LocalEmbeddingProvider
        A zero-dependency, deterministic hashed-unigram embedding. It is a real,
        repeatable vectorizer (same text → identical vector on every machine and run)
        intended for basic similarity search and bootstrapped retrieval. It is NOT an
        ML model and is not semantically deep — that is fine for V1, and the interface
        is exactly what a future provider (ChromaDB default, sentence-transformers,
        an API model) will implement.

Failure rules (hard requirement):
  - If embedding generation fails, log, preserve the record, and allow non-semantic
    retrieval. NEVER crash VERONICA because a vector could not be produced.
  - ``LocalEmbeddingProvider`` cannot fail-by-dependency — it needs nothing — but the
    store still guards any ``embed()``/``embed_batch()`` call so a future provider's
    failure degrades the same way.
"""
from __future__ import annotations

import hashlib
import re
from typing import List, Optional, Sequence

from veronica.logging_setup import get_logger

logger = get_logger("memory.thread.embeddings")

# Embedding dimensionality for the local provider. Kept small so the pure-Python cosine
# similarity stays fast; sentinel ``None`` means a provider of arbitrary dimension.
DEFAULT_DIM = 128

_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+", re.UNICODE)


def _stable_token_hash(token: str) -> int:
    """Deterministic 32-bit digest of a token (Python's builtin hash() is salted per
    process — unusable for stable embeddings)."""
    return int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:4], "little")


class EmbeddingProvider:
    """Interface for text→vector embedding. All VERONICA layers depend on this, never on
    a concrete backend. Implementations must be deterministic for equal inputs."""

    name: str = "base"
    dimension: Optional[int] = None

    def is_available(self) -> bool:
        """True when this provider can embed now. A provider may be unavailable at
        runtime (missing model file, API down) — callers must degrade, never crash."""
        return True

    def embed(self, text: str) -> List[float]:
        """Embed one text string into a flat list of floats. Raises nothing on failure;
        implementations return a zero-ish vector and log when they cannot embed."""
        raise NotImplementedError

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        """Embed many texts in one call. Same failure contract as ``embed``."""
        return [self.embed(t) for t in texts]

    def describe(self) -> dict:
        return {"provider": self.name, "dimension": self.dimension}


class LocalEmbeddingProvider(EmbeddingProvider):
    """Deterministic hashed-unigram embedding. Pure Python, zero dependencies.

    Building a vector:
        tokenize -> lowercase tokens -> for each token, set ``sign * position`` buckets
        of the 128-dim vector (two random-origin buckets per token, sign derived from a
        second stable hash). The result is a sparse signed bag-of-words embedding.

    Same text on any machine, any process, any run → identical vector. Different text
    → different-but-comparable vectors. Enough for V1 keyword-ish relevance; swap in an
    ML provider later without touching any caller.
    """

    name = "local_hashed"
    dimension = DEFAULT_DIM

    def __init__(self, dimension: int = DEFAULT_DIM) -> None:
        self.dimension = dimension

    def embed(self, text: str) -> List[float]:
        vec = [0.0] * self.dimension
        if not text:
            return vec
        for token in _TOKEN_RE.findall(text.lower()):
            h1 = _stable_token_hash(token)
            h2 = _stable_token_hash(token + "\x00salt")
            # Scatter across the vector: two bucket indices + a sign, from two stable
            # hashes. Collisions only soften discrimination; they cannot break retrieval.
            for h in (h1, h2):
                idx = h % self.dimension
                sign = 1.0 if (h >> 16) & 1 else -1.0
                vec[idx] += sign * (1.0 + (h & 0xFFFF) / 65535.0)
        return vec

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]


def make_embedding_provider(name: str = "local") -> EmbeddingProvider:
    """Factory. Unknown names fall back to local and log a warning — configuration must
    never break the assistant."""
    if name in ("local", "local_hashed", ""):
        return LocalEmbeddingProvider()
    logger.warning("Unknown embedding provider %r; falling back to 'local'.", name)
    return LocalEmbeddingProvider()


__all__ = ["EmbeddingProvider", "LocalEmbeddingProvider", "make_embedding_provider"]