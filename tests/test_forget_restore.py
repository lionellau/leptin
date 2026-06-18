"""Glass-box reversibility (PRD 8.5)."""

from __future__ import annotations


def test_forget_then_restore(mem):
    r = mem.remember("Restorable fact about backups.", subject="ops")
    out = mem.forget(memory_id=r["memory_id"])
    assert out["count"] == 1
    assert mem.inspect(memory_id=r["memory_id"])["status"] == "quarantined"

    # Forgotten memory is not recalled.
    res = mem.recall("backups")
    assert not any(m["memory_id"] == r["memory_id"] for m in res["memories"])

    # Restore brings it back and it is recallable again.
    restored = mem.restore(r["memory_id"])
    assert restored["restored"] is True
    assert mem.inspect(memory_id=r["memory_id"])["status"] == "active"
    res2 = mem.recall("backups")
    assert any("backups" in m["content"] for m in res2["memories"])


def test_forget_by_query(mem):
    mem.remember("The secret office wifi password is hunter2.", subject="wifi")
    out = mem.forget(query="office wifi password")
    assert out["count"] >= 1
    res = mem.recall("wifi password")
    assert not any("hunter2" in m["content"] for m in res["memories"])


def test_restore_missing_returns_false(mem):
    assert mem.restore("does-not-exist")["restored"] is False


def test_restore_already_active_returns_false(mem):
    r = mem.remember("Active fact.", subject="x")
    assert mem.restore(r["memory_id"])["restored"] is False


def test_inspect_exposes_provenance_and_events(mem):
    r = mem.remember("Inspectable fact.", subject="x", source="unit-test")
    info = mem.inspect(memory_id=r["memory_id"])
    assert info["memory"]["content"] == "Inspectable fact."
    assert info["provenance"]["provenance"] == "unit-test"
    assert any(e["type"] == "create" for e in info["events"])
