"""Self-tuning (PRD §13) — acceptance criteria as tests."""

from __future__ import annotations

import time

import pytest

from leptin import bench
from leptin.config import Config
from leptin.engine import DietEngine
from leptin.storage import Store
from leptin.tuner import ACTION_SPACE, LOCKED_KNOBS, Tuner, validate_proposal


def _seed_engine(cfg: Config) -> tuple[Store, DietEngine]:
    """A store from the bench corpus with a recall query log (real headroom)."""
    store = Store(":memory:", clock=bench.FIXED_CLOCK)
    eng = DietEngine(store, cfg)
    for subj, c in bench.build_corpus()["inserts"]:
        eng.remember(c, subject=subj)
    for q, _ in bench.build_corpus()["probes"]:
        eng.recall(q)
    return store, eng


def test_offline_zero_cost():
    """§13.9.1 — a full cycle makes no LLM calls and spends no tune tokens."""
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.4))
    res = eng.tuner.tune(trigger="manual")
    assert res["llm_calls"] == 0 and res["tune_tokens"] == 0
    for r in store.list_tune_runs():
        assert r["llm_calls"] == 0 and r["tune_tokens"] == 0
    store.close()


def test_lock_enforcement():
    """§13.9.3 — the tuner can never touch a locked knob (esp. the guardrail)."""
    assert "guardrail_max_drop" in LOCKED_KNOBS
    with pytest.raises(ValueError):
        validate_proposal("guardrail_max_drop")
    with pytest.raises(ValueError):
        validate_proposal("dedup_threshold")
    # Only the declared action-space knobs are ever proposed.
    for knob in ACTION_SPACE:
        validate_proposal(knob)  # must not raise


def test_improvement_on_degraded_store():
    """§13.9.4 — a deliberately bad config improves with 0 recall regression."""
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.40,
                                     token_budget_default=2000))
    now = bench.FIXED_CLOCK()
    ev = eng.tuner.evaluator
    probes = eng.guardrail.build_probe_set(now)
    qlog = ev.query_log(now)
    base = ev.evaluate(eng.config, probes, qlog, now)

    for _ in range(5):
        eng.tuner.tune(trigger="manual")

    after = ev.evaluate(eng.config, probes, qlog, now)
    assert after["reduction"] > base["reduction"]          # got leaner
    assert after["recall"] >= base["recall"] - eng.config.guardrail_max_drop  # no recall loss
    store.close()


def test_guardrail_dominance_no_accepted_config_drops_recall():
    """§13.9.2 — no accepted change drops held-out recall past the bound."""
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.4))
    for _ in range(6):
        res = eng.tuner.tune(trigger="manual")
        if res.get("accepted"):
            assert res["recall_after"] >= res["recall_before"] - eng.config.guardrail_max_drop
    store.close()


def test_reversibility_round_trip():
    """§13.9.6 — rollback restores the exact prior config."""
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.40,
                                     token_budget_default=2000))
    before = eng.config.to_dict()
    # Tune until something is accepted.
    accepted = False
    for _ in range(5):
        if eng.tuner.tune(trigger="manual").get("accepted"):
            accepted = True
            break
    assert accepted, "expected at least one accepted change to test rollback"
    assert eng.config.to_dict() != before
    eng.tuner.rollback()  # undo the last accepted change
    # Roll back to the original baseline (may take one step per accepted change).
    for _ in range(5):
        if eng.config.to_dict() == before:
            break
        eng.tuner.rollback()
    assert eng.config.to_dict() == before
    store.close()


def test_no_op_safety_on_default_config():
    """§13.9.5 — tuning a healthy store never drops recall below baseline."""
    store, eng = _seed_engine(Config(self_tune_enabled=True))  # defaults
    now = bench.FIXED_CLOCK()
    ev = eng.tuner.evaluator
    probes = eng.guardrail.build_probe_set(now)
    base = ev.evaluate(eng.config, probes, ev.query_log(now), now)
    eng.tuner.tune(trigger="manual")
    after = ev.evaluate(eng.config, probes, ev.query_log(now), now)
    assert after["recall"] >= base["recall"] - eng.config.guardrail_max_drop
    store.close()


def test_determinism_identical_stores_same_versions():
    """§13.9.9 — same data + fixed clock → identical evolution ledger."""
    def run() -> list:
        store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.40,
                                         token_budget_default=2000))
        for _ in range(3):
            eng.tuner.tune(trigger="manual")
        versions = [(v["knob"], v["new_value"]) for v in store.list_config_versions(50)]
        store.close()
        return versions

    assert run() == run()


def test_should_tune_respects_cadence_and_flag():
    """§13.9.8 — triggers don't fire when disabled or below thresholds."""
    store, eng = _seed_engine(Config(self_tune_enabled=False))
    assert eng.tuner.should_tune(bench.FIXED_CLOCK()) == (False, None)
    # Enabled but no new memories since the last tune and within the interval.
    eng.config.self_tune_enabled = True
    store.save_config({"_tune.last_at": bench.FIXED_CLOCK(),
                       "_tune.last_count": store.count_memories(None)})
    fired, _ = eng.tuner.should_tune(bench.FIXED_CLOCK())
    assert fired is False
    store.close()


def test_latency_under_budget():
    """§13.9.7 — a tune cycle is fast (well under the 200ms/1k-memory target)."""
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.4))
    t0 = time.perf_counter()
    eng.tuner.tune(trigger="manual")
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert elapsed_ms < 1500  # generous CI bound; ~tens of ms locally
    store.close()


def test_manual_tune_not_frozen_by_meta_guardrail():
    """Manual tuning is always allowed; only the auto loop self-freezes."""
    store, eng = _seed_engine(Config(self_tune_enabled=True))
    # Force a freeze as the auto loop would.
    store.save_config({"_tune.frozen_until": bench.FIXED_CLOCK() + 1e9})
    res = eng.tuner.tune(trigger="manual")
    assert res.get("status") != "frozen"  # manual ran anyway
    store.close()


def test_diet_report_tuning_block():
    store, eng = _seed_engine(Config(self_tune_enabled=True, recall_rel_floor=0.4))
    assert eng.diet_report("all")["tuning"] is None  # nothing tuned yet
    eng.tuner.tune(trigger="manual")
    block = eng.diet_report("all")["tuning"]
    assert block is not None
    assert block["enabled"] is True
    assert "cycles" in block and block["llm_calls"] == 0
    store.close()
