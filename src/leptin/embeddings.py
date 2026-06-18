"""Embedding backends.

Default: ``LocalHashingEmbedder`` — a deterministic, dependency-free bag-of-
n-grams hashing vectorizer. It captures lexical overlap well enough to detect
near-duplicate restatements and to drive a reproducible offline benchmark.

Upgrade: ``HostedEmbedder`` calls OpenAI / Voyage when an API key is present.
Both implement the same tiny interface, so the engine never changes.

Graceful degradation: if a hosted call fails, the engine falls back to the
local embedder rather than throwing to the agent.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_WORD = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int
    name: str

    def embed(self, text: str) -> list[float]: ...


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Assumes equal length; returns 0.0 for zero vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _tokens(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _char_ngrams(word: str, n: int = 3) -> list[str]:
    padded = f"#{word}#"
    if len(padded) <= n:
        return [padded]
    return [padded[i : i + n] for i in range(len(padded) - n + 1)]


class LocalHashingEmbedder:
    """Deterministic hashing embedder — no network, no model download.

    Features = unigram words + word bigrams + character trigrams, each hashed
    into ``dim`` buckets with a signed hash, then L2-normalized. This makes
    cosine similarity track shared vocabulary and short phrases, which is what
    near-duplicate detection needs.
    """

    name = "local-hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _hash(self, feature: str) -> tuple[int, float]:
        h = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % self.dim
        sign = 1.0 if (h[4] & 1) else -1.0
        return idx, sign

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = _tokens(text)
        if not words:
            return vec
        features: list[str] = []
        features.extend(words)
        features.extend(f"{a}_{b}" for a, b in zip(words, words[1:]))
        for w in words:
            features.extend(f"c:{g}" for g in _char_ngrams(w))
        for feat in features:
            idx, sign = self._hash(feat)
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm == 0.0:
            return vec
        return [v / norm for v in vec]


class HostedEmbedder:
    """OpenAI / Voyage embeddings via their SDKs. Used only when a key is set."""

    def __init__(self, model: str, dim: int = 1536):
        self.name = model
        self.model = model
        self.dim = dim

    def embed(self, text: str) -> list[float]:  # pragma: no cover - needs network
        if self.model.startswith("voyage"):
            import voyageai

            client = voyageai.Client()
            result = client.embed([text], model=self.model)
            return result.embeddings[0]
        from openai import OpenAI

        client = OpenAI()
        resp = client.embeddings.create(model=self.model, input=text)
        return resp.data[0].embedding


def make_embedder(model: str, dim: int = 256) -> Embedder:
    """Factory: pick the embedder for a model name, defaulting to local/offline."""
    if not model or model in ("local-hash", "heuristic", "offline"):
        return LocalHashingEmbedder(dim=dim)
    if model.startswith(("text-embedding", "voyage")):
        return HostedEmbedder(model)
    return LocalHashingEmbedder(dim=dim)
