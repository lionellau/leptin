"""Savings ledger + diet_report (PRD 8.3)."""

from __future__ import annotations

from leptin.config import Config


def test_ledger_tokens_saved_equals_sum_of_deltas(mem):
    mem.remember("Fact A about something.", subject="a")
    mem.remember("Fact A about something.", subject="a")  # merge → footprint
    mem.recall("something")
    rows = mem.store.ledger_rows()
    report = mem.diet_report("all")
    # Headline savings == sum of recall (injection) savings across rows.
    assert report["tokens_saved"] == sum(r["tokens_saved"] for r in rows)


def test_ledger_usd_matches_price_table(mem):
    cfg = mem.config
    mem.remember("Repeatable fact.", subject="x")
    mem.remember("Repeatable fact.", subject="x")
    report = mem.diet_report("all")
    expected_usd = cfg.usd_for_tokens(report["tokens_saved"])
    assert abs(report["usd_saved"] - expected_usd) < 1e-6


def test_merge_counts_as_footprint_not_headline(mem):
    mem.remember("The user prefers dark mode.", subject="prefs")
    mem.remember("The user prefers dark mode.", subject="prefs")  # exact dup → merge
    report = mem.diet_report("session")
    assert report["ops"]["merged"] >= 1
    # A merge is a one-time storage reduction, not headline injection savings.
    assert report["footprint_tokens_reduced"] > 0
    assert report["model"] == mem.config.price_model


def test_recall_savings_drive_headline(mem):
    # Distinct-but-topical deployment facts (won't merge) so a budgeted recall
    # genuinely injects fewer tokens than a naive top-k dump.
    facts = [
        "The deployment pipeline uses blue-green releases for zero downtime.",
        "Deployment rollbacks are triggered automatically by a failing health check.",
        "The deploy pipeline runs smoke tests after every production release.",
        "Production deployments require two reviewer approvals before shipping.",
        "Deployment status notifications are posted to the operations channel.",
        "The deployment window is restricted to weekday business hours.",
        "Canary deployments route five percent of traffic before full rollout.",
        "Deployment artifacts are built once and promoted across environments.",
        "Each deployment is tagged with the git commit and a build number.",
        "Deployment secrets are injected from the vault at release time.",
        "A deployment freeze is enforced during the end-of-quarter close.",
        "Deployment metrics are recorded to track lead time and failure rate.",
    ]
    for i, f in enumerate(facts):
        mem.remember(f, subject=f"deploy{i}")  # distinct subjects → no merging
    mem.recall("deployment release pipeline", token_budget=60)
    report = mem.diet_report("all")
    assert report["tokens_saved"] > 0
    assert report["usd_saved"] >= 0


def test_forget_is_recorded_in_ledger(mem):
    r = mem.remember("Forget this later.", subject="x")
    out = mem.forget(memory_id=r["memory_id"])
    assert out["count"] == 1
    report = mem.diet_report("all")
    assert report["ops"]["forgotten"] >= 1


def test_window_filtering(mem):
    mem.remember("Windowed fact.", subject="x")
    mem.remember("Windowed fact.", subject="x")
    all_rows = mem.diet_report("all")["tokens_saved"]
    session_rows = mem.diet_report("session")["tokens_saved"]
    # Everything happened in this session, so the two agree here.
    assert all_rows == session_rows >= 0


def test_price_table_custom_model(make_mem):
    cfg = Config(price_model="gpt-4o-mini")
    m = make_mem(cfg)
    m.remember("priced fact", subject="x")
    m.remember("priced fact", subject="x")
    report = m.diet_report("all")
    assert report["model"] == "gpt-4o-mini"
    assert report["usd_saved"] == round(cfg.usd_for_tokens(report["tokens_saved"]), 6)
