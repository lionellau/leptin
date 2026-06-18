"""Reproducible benchmark headline (PRD 8.8 / Goal 1)."""

from __future__ import annotations

from leptin import bench


def test_headline_reproduces():
    r = bench.run()
    assert r["token_reduction_pct"] >= 60.0          # ≥60% fewer memory tokens
    assert r["recall_loss_pct"] <= 2.0               # at ≤2% recall loss
    assert r["headline_pass"] is True


def test_benchmark_is_deterministic():
    a = bench.run()
    b = bench.run()
    assert a["token_reduction_pct"] == b["token_reduction_pct"]
    assert a["leptin_tokens"] == b["leptin_tokens"]
    assert a["naive_tokens"] == b["naive_tokens"]


def test_dedup_shrinks_store():
    r = bench.run()
    # Leptin keeps fewer active memories than the naive store.
    assert r["leptin_active_memories"] < r["naive_active_memories"]


def test_table_renders():
    r = bench.run()
    table = bench.format_table(r)
    assert "TOKEN REDUCTION" in table
    assert "HEADLINE" in table
