"""Recall guardrail — the differentiator (PRD 8.4)."""

from __future__ import annotations

from leptin.config import Config


def test_compact_rolls_back_when_recall_would_drop(make_mem, clock):
    """A user-probed fact that would be pruned must trigger a rollback."""
    m = make_mem(Config(decay_half_life_days=10.0))
    r = m.remember("The encryption key rotates every 90 days.", subject="security")
    m.add_probe("how often does the encryption key rotate?", "90 days")
    clock.advance_days(60)  # decays below the strength floor

    res = m.compact()
    assert res["decayed"] == 1
    assert res["guardrail"]["rolled_back"] is True
    assert res["guardrail"]["passed"] is False
    # The prune was undone — the memory is still active and recallable.
    assert m.inspect(memory_id=r["memory_id"])["status"] == "active"
    recalled = m.recall("encryption key rotation")
    assert any("90 days" in mm["content"] for mm in recalled["memories"])


def test_compact_commits_safe_prune(make_mem, clock):
    """A stale, unprobed memory is pruned while a fresh one is preserved."""
    m = make_mem(Config(decay_half_life_days=10.0))
    fresh = m.remember("Alice leads the design team.", subject="people")
    stale = m.remember("The old marketing slogan was 'Think Big'.", subject="marketing")
    clock.advance_days(60)
    m.recall("who leads design?")  # reinforce the fresh memory

    res = m.compact()
    assert res["guardrail"]["passed"] is True
    assert res["guardrail"]["rolled_back"] is False
    # Invariant from PRD 8.4: recall_after >= recall_before - max_drop
    g = res["guardrail"]
    assert g["recall_after"] >= g["recall_before"] - g["max_drop"]
    assert m.inspect(memory_id=fresh["memory_id"])["status"] == "active"
    assert m.inspect(memory_id=stale["memory_id"])["status"] == "quarantined"


def test_compact_dry_run_does_not_commit(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    s = m.remember("Ephemeral fact.", subject="x")
    clock.advance_days(60)
    res = m.compact(dry_run=True)
    assert res["dry_run"] is True
    # Nothing committed — the memory is untouched.
    assert m.inspect(memory_id=s["memory_id"])["status"] == "active"


def test_compact_noop_when_nothing_to_prune(mem):
    mem.remember("A perfectly healthy memory.", subject="x")
    res = mem.compact()
    assert res["decayed"] == 0
    assert res["guardrail"]["passed"] is True
    assert res["guardrail"]["rolled_back"] is False


def test_guardrail_not_fooled_by_token_sharing_survivor(make_mem, clock):
    """Regression: an unrelated survivor sharing tokens with a probed fact must
    NOT mask the loss of that fact (identity-based coverage)."""
    m = make_mem(Config(decay_half_life_days=10.0))
    sec = m.remember("The encryption key rotates every 90 days.", subject="security")
    m.add_probe("how often does the encryption key rotate?", "90 days")
    # A semantically unrelated memory that happens to contain "90 days".
    m.remember("The free trial period lasts 90 days for new users.", subject="billing")
    clock.advance_days(60)
    m.recall("free trial period")  # keep the billing memory fresh

    res = m.compact()
    assert res["guardrail"]["rolled_back"] is True
    assert m.inspect(memory_id=sec["memory_id"])["status"] == "active"


def test_expired_quarantine_is_purged_and_not_restorable(make_mem, clock):
    m = make_mem(Config(reversible_window_days=7.0))
    r = m.remember("Temporary fact.", subject="x")
    m.forget(memory_id=r["memory_id"])
    clock.advance_days(10)            # past the reversible window
    m.compact()                       # purges expired quarantine
    assert m.inspect(memory_id=r["memory_id"])["status"] == "deleted"
    assert m.restore(r["memory_id"])["restored"] is False


def test_probe_run_recorded(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    m.remember("Trackable fact.", subject="x")
    clock.advance_days(60)
    m.compact()
    runs = m.store.conn.execute("SELECT COUNT(*) FROM probe_runs").fetchone()[0]
    assert runs >= 1
