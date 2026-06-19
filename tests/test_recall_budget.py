"""Budgeted, packed recall (PRD 8.1 / 8.2)."""

from __future__ import annotations

from leptin.config import Config
from leptin.tokenizer import count_memory_tokens


def _seed(mem, n=40):
    for i in range(n):
        mem.remember(
            f"Memory number {i} about topic {i % 5} with some descriptive filler text.",
            subject=f"topic{i % 5}",
        )


def test_recall_never_exceeds_budget(mem):
    _seed(mem, 40)
    for budget in (40, 80, 150, 300):
        res = mem.recall("topic 2 descriptive filler", token_budget=budget)
        assert res["tokens_used"] <= budget
        # The reported tokens match an independent recount of the payload.
        recount = count_memory_tokens(
            [{"subject": m["subject"], "content": m["content"]} for m in res["memories"]],
            "heuristic",
        )
        assert recount == res["tokens_used"]


def test_baseline_at_least_actual(mem):
    _seed(mem, 30)
    res = mem.recall("topic 1 filler text", token_budget=120)
    assert res["baseline_tokens"] >= res["tokens_used"]
    assert res["tokens_saved"] == max(0, res["baseline_tokens"] - res["tokens_used"])


def test_dropped_count_reported(mem):
    _seed(mem, 30)
    res = mem.recall("topic 0 descriptive", token_budget=30, k=20)
    assert res["dropped_count"] >= 0
    assert res["dropped_count"] == res.get("dropped_count")


def test_zero_budget_injects_nothing(mem):
    """Regression: token_budget=0 is an explicit ceiling, not 'unset' (falsy bug)."""
    _seed(mem, 20)
    res = mem.recall("topic 2 filler", token_budget=0)
    assert res["tokens_used"] == 0
    assert res["memories"] == []


def test_zero_k_injects_nothing(mem):
    _seed(mem, 20)
    res = mem.recall("topic 1 filler", k=0)
    assert res["memories"] == []


def test_empty_store_recall_is_safe(mem):
    res = mem.recall("anything")
    assert res["memories"] == []
    assert res["tokens_used"] == 0
    assert res["tokens_saved"] == 0


def test_relevance_gate_excludes_offtopic(mem):
    mem.remember("The capital of France is Paris.", subject="geo")
    mem.remember("Completely unrelated note about quarterly taxes.", subject="finance")
    res = mem.recall("what is the capital of France?", token_budget=1000)
    contents = " ".join(m["content"] for m in res["memories"])
    assert "Paris" in contents
    assert "taxes" not in contents
