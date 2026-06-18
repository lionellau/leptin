"""Storage layer: CRUD, events, transactions (PRD 9)."""

from __future__ import annotations

from leptin.storage import Store


def test_add_and_get_memory():
    s = Store(":memory:")
    m = s.add_memory("hello", [0.1, 0.2], tokens=3, subject="x")
    got = s.get_memory(m["id"])
    assert got["content"] == "hello"
    assert got["embedding"] == [0.1, 0.2]
    assert got["status"] == "active"
    s.close()


def test_update_memory_serializes_embedding():
    s = Store(":memory:")
    m = s.add_memory("hi", [1.0], tokens=1)
    s.update_memory(m["id"], embedding=[2.0, 3.0], tokens=2)
    got = s.get_memory(m["id"])
    assert got["embedding"] == [2.0, 3.0]
    assert got["tokens"] == 2
    s.close()


def test_status_filtering_and_counts():
    s = Store(":memory:")
    a = s.add_memory("a", [], tokens=1)
    s.add_memory("b", [], tokens=1)
    s.update_memory(a["id"], status="quarantined")
    assert s.count_memories("active") == 1
    assert s.count_memories("quarantined") == 1
    assert s.count_memories(None) == 2
    s.close()


def test_events_appended_in_order():
    s = Store(":memory:")
    m = s.add_memory("x", [], tokens=1)
    s.add_event(m["id"], "create", "first")
    s.add_event(m["id"], "recall_inject", "second")
    evs = s.events_for(m["id"])
    assert [e["type"] for e in evs] == ["create", "recall_inject"]
    s.close()


def test_transaction_rollback_undoes_changes():
    s = Store(":memory:")
    m = s.add_memory("keep me", [], tokens=1)
    s.begin()
    s.update_memory(m["id"], status="quarantined")
    assert s.get_memory(m["id"])["status"] == "quarantined"  # visible in txn
    s.rollback()
    assert s.get_memory(m["id"])["status"] == "active"  # rolled back
    s.close()


def test_transaction_commit_persists():
    s = Store(":memory:")
    m = s.add_memory("change me", [], tokens=1)
    s.begin()
    s.update_memory(m["id"], status="quarantined")
    s.commit()
    assert s.get_memory(m["id"])["status"] == "quarantined"
    s.close()


def test_config_roundtrip():
    s = Store(":memory:")
    s.save_config({"token_budget_default": 999, "price_model": "gpt-4o"})
    loaded = s.load_config()
    assert loaded["token_budget_default"] == 999
    assert loaded["price_model"] == "gpt-4o"
    s.close()


def test_ledger_since_filter():
    s = Store(":memory:")
    s.add_ledger("recall", 100, 40, 60, "m", 0.001, "sess1")
    rows = s.ledger_rows()
    assert len(rows) == 1
    assert rows[0]["tokens_saved"] == 60
    s.close()


def test_probes_crud():
    s = Store(":memory:")
    pid = s.add_probe("q?", "answer")
    assert len(s.list_probes()) == 1
    s.clear_probes()
    assert s.list_probes() == []
    assert pid
    s.close()
