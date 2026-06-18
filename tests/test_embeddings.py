"""Embedding backend + similarity (PRD 7.1)."""

from __future__ import annotations

from leptin.embeddings import LocalHashingEmbedder, cosine, make_embedder


def test_local_embedder_deterministic():
    e = LocalHashingEmbedder(dim=128)
    v1 = e.embed("the quick brown fox")
    v2 = e.embed("the quick brown fox")
    assert v1 == v2
    assert len(v1) == 128


def test_identical_text_cosine_is_one():
    e = LocalHashingEmbedder()
    v = e.embed("identical sentence here")
    assert cosine(v, v) == 1.0 or abs(cosine(v, v) - 1.0) < 1e-9


def test_related_more_similar_than_unrelated():
    e = LocalHashingEmbedder()
    base = e.embed("the backend uses postgres database")
    near = e.embed("the backend uses a postgres database")
    far = e.embed("the user enjoys hiking on weekends")
    assert cosine(base, near) > cosine(base, far)


def test_empty_text_returns_zero_vector():
    e = LocalHashingEmbedder(dim=16)
    v = e.embed("")
    assert v == [0.0] * 16
    assert cosine(v, v) == 0.0


def test_make_embedder_defaults_to_local():
    assert isinstance(make_embedder("local-hash"), LocalHashingEmbedder)
    assert isinstance(make_embedder(""), LocalHashingEmbedder)
    assert isinstance(make_embedder("heuristic"), LocalHashingEmbedder)


def test_cosine_handles_length_mismatch():
    assert cosine([1.0, 2.0], [1.0]) == 0.0
    assert cosine([], [1.0]) == 0.0
