"""Diet-engine behaviour: dedup/merge, supersede, decay, degradation."""

from __future__ import annotations

import pytest

from leptin.config import Config
from leptin.embeddings import LocalHashingEmbedder


def test_create_then_merge_exact_duplicate(mem):
    r1 = mem.remember("The deploy target is Fly.io.", subject="infra")
    assert r1["action"] == "created"
    assert r1["tokens_saved"] == 0

    r2 = mem.remember("The deploy target is Fly.io.", subject="infra")
    assert r2["action"] == "merged"
    assert r2["memory_id"] == r1["memory_id"]  # merged into the original
    assert r2["tokens_saved"] > 0              # PRD 8.1: positive tokens_saved

    # Only one active memory remains.
    actives = mem.store.list_memories("active")
    assert len(actives) == 1


def test_contradiction_supersedes_not_deletes(mem):
    r1 = mem.remember("The server listens on port 8080.", subject="net")
    r2 = mem.remember("The server listens on port 9090.", subject="net")
    assert r2["action"] == "superseded"

    # Newer is active; older is superseded but NOT deleted (PRD 8.2).
    older = mem.inspect(memory_id=r1["memory_id"])
    assert older["status"] == "superseded"
    newer = mem.inspect(memory_id=r2["memory_id"])
    assert newer["status"] == "active"

    # Recall surfaces the newer fact.
    res = mem.recall("what port does the server use?")
    contents = " ".join(m["content"] for m in res["memories"])
    assert "9090" in contents


def test_supersede_removes_all_stale_versions(make_mem):
    # When multiple distinct stale versions coexist, the newer contradicting fact
    # supersedes ALL of them. Dedup off here so the two near-identical phrasings
    # stay separate — with dedup on they'd merge first, which is also correct.
    from leptin.config import Config

    mem = make_mem(Config(dedup_threshold=2.0))
    mem.remember("The trial lasts 14 days.", subject="billing")
    mem.remember("The free trial lasts 14 days.", subject="billing")
    r = mem.remember("The trial now lasts 30 days.", subject="billing")
    assert r["action"] == "superseded"
    assert len(r["superseded"]) >= 2
    actives = [m["content"] for m in mem.store.list_memories("active")]
    assert any("30 days" in c for c in actives)
    assert not any("14 days" in c for c in actives)


def test_supersede_replaces_stale_value(mem):
    mem.remember("The trial lasts 14 days.", subject="billing")
    r = mem.remember("The trial lasts 30 days.", subject="billing")
    assert r["action"] == "superseded"
    res = mem.recall("how long is the trial?")
    joined = " ".join(m["content"] for m in res["memories"])
    assert "30 days" in joined and "14 days" not in joined


def test_different_subjects_do_not_merge(mem):
    r1 = mem.remember("Status is green.", subject="alpha")
    r2 = mem.remember("Status is green.", subject="beta")
    assert r2["action"] == "created"
    assert r1["memory_id"] != r2["memory_id"]
    assert mem.store.count_memories("active") == 2


def test_empty_content_skipped(mem):
    r = mem.remember("   ")
    assert r["action"] == "skipped"
    assert mem.store.count_memories("active") == 0


def test_decay_reduces_strength(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    r = m.remember("A fact that will age.", subject="x")
    s0 = m.inspect(memory_id=r["memory_id"])["strength"]
    clock.advance_days(10)
    s1 = m.inspect(memory_id=r["memory_id"])["strength"]
    assert s1 == pytest.approx(s0 * 0.5, abs=0.05)


def test_access_boosts_strength(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    m.remember("Frequently used fact about caching.", subject="x")
    clock.advance_days(10)
    before = m.recall("caching")  # boosts on access
    clock.advance(1)
    after = m.recall("caching")
    # Strength after a recent access should be high again.
    assert after["memories"][0]["strength"] > 0.5


class _EmptyEmbedder:
    """Simulates an embedding API that returns nothing usable."""

    name = "down"
    dim = 0

    def embed(self, text):
        return []


class _RaisingEmbedder:
    name = "hosted-down"
    dim = 1536

    def embed(self, text):
        raise RuntimeError("embedding API unreachable")


def test_graceful_degradation_empty_embedder(make_mem):
    """PRD 8.1: with embeddings unavailable, ops degrade, never throw."""
    from leptin.engine import DietEngine
    from leptin.storage import Store

    store = Store(":memory:")
    engine = DietEngine(store, Config(), embedder=_EmptyEmbedder())
    r = engine.remember("Store me even without embeddings.", subject="x")
    assert r["action"] == "created"  # stored raw
    res = engine.recall("store me")  # keyword fallback, no exception
    assert isinstance(res["memories"], list)
    assert any("Store me" in m["content"] for m in res["memories"])
    store.close()


def test_graceful_degradation_raising_embedder(make_mem):
    from leptin.engine import DietEngine
    from leptin.storage import Store

    store = Store(":memory:")
    engine = DietEngine(store, Config(), embedder=_RaisingEmbedder())
    # Must not raise — falls back to the local embedder internally.
    r = engine.remember("Resilient fact.", subject="x")
    assert r["action"] in ("created", "merged")
    res = engine.recall("resilient")
    assert isinstance(res, dict)
    store.close()
