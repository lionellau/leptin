"""Config + env coercion (regression for the env-string crash)."""

from __future__ import annotations

from leptin.config import Config


def test_env_coerces_floats(monkeypatch):
    monkeypatch.setenv("LEPTIN_RECALL_REL_FLOOR", "0.7")
    monkeypatch.setenv("LEPTIN_RECALL_MIN_SIM", "0.1")
    monkeypatch.setenv("LEPTIN_TOKEN_BUDGET_DEFAULT", "800")
    c = Config.from_env()
    assert isinstance(c.recall_rel_floor, float) and c.recall_rel_floor == 0.7
    assert isinstance(c.recall_min_sim, float) and c.recall_min_sim == 0.1
    assert isinstance(c.token_budget_default, int) and c.token_budget_default == 800


def test_env_float_override_does_not_crash_recall(monkeypatch):
    from leptin.api import Leptin

    monkeypatch.setenv("LEPTIN_RECALL_REL_FLOOR", "0.6")
    mem = Leptin(":memory:", Config.from_env())
    mem.remember("The backend is FastAPI on Postgres.", subject="stack")
    res = mem.recall("what is the backend")  # must not raise TypeError
    assert isinstance(res["memories"], list)
    mem.close()


def test_env_invalid_value_is_ignored(monkeypatch):
    monkeypatch.setenv("LEPTIN_DEDUP_THRESHOLD", "not-a-number")
    c = Config.from_env()
    assert c.dedup_threshold == Config().dedup_threshold  # kept default


def test_env_dict_parsed_as_json(monkeypatch):
    monkeypatch.setenv("LEPTIN_PRICE_TABLE", '{"x": {"input": 1.0, "output": 2.0}}')
    c = Config.from_env()
    assert c.price_table["x"]["input"] == 1.0


def test_usd_for_tokens_uses_price_model():
    c = Config(price_model="gpt-4o-mini")
    # gpt-4o-mini input price is 0.15 / 1M tokens.
    assert abs(c.usd_for_tokens(1_000_000) - 0.15) < 1e-9
