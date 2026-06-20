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
    assert "token reduction" in table
    assert "HEADLINE" in table


def test_token_reduction_splits_packing_vs_governance():
    """The headline must NOT let the packing axis (which a compressor also helps
    with) stand in for the correctness loop — report both, separately."""
    r = bench.run()
    assert "packing_reduction_pct" in r and "governance_reduction_pct" in r
    assert r["governance_reduction_pct"] > 0  # dedup/supersede genuinely contributes
    assert r["gated_tokens"] >= r["leptin_tokens"]  # governance only shrinks further


def test_correctness_bench_naive_serves_stale_leptin_does_not():
    """The wedge, measured: a naive store serves the OUTDATED fact after a reversal;
    Leptin serves the current one."""
    c = bench.run_correctness()
    assert c["naive_stale_rate"] >= 0.9      # naive keeps serving the stale fact
    assert c["leptin_stale_rate"] <= 0.1     # Leptin supersedes it
    assert c["leptin_current_coverage"] >= 0.9
    assert c["pass"] is True


def test_contradiction_detector_precision_recall():
    """The detector must NOT bury true facts (precision 1.0 = zero false supersedes)
    and should catch the confident offline cases (recall well above chance)."""
    e = bench.eval_contradiction()
    assert e["precision"] == 1.0             # no true fact ever buried
    assert e["recall"] >= 0.75
    assert e["fp"] == 0
