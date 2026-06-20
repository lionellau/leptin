"""v1.2 — the recall-usefulness flywheel, feedback, auto mistake-capture, health."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from leptin.config import Config

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def test_recall_increments_inject_count(mem):
    r = mem.remember("The deploy region is us-west-2.", subject="infra")
    mem.recall("which region for deploys?")
    assert mem.inspect(memory_id=r["memory_id"])["memory"]["inject_count"] >= 1


def test_recurrence_across_sessions_marks_recurrence_not_useful(make_mem, clock):
    from leptin.api import Leptin
    # Same on-disk store, two different sessions (fresh Leptin → new session id).
    import tempfile
    db = tempfile.mktemp(suffix=".db")
    a = Leptin(db, Config(), clock=clock, session_id="session-A")
    r = a.remember("Prod secrets live in AWS Secrets Manager.", subject="infra")
    a.recall("where are prod secrets?")
    a.close()
    clock.advance(1)
    b = Leptin(db, Config(), clock=clock, session_id="session-B")  # a different session
    b.recall("where are prod secrets?")     # needed again in a later session → recurrence
    info = b.inspect(memory_id=r["memory_id"])["memory"]
    # Recurrence is a WEAK signal (recur_sessions), NOT proof it helped: useful_count
    # stays 0 until explicit feedback. This is the v1.3 flywheel fix.
    assert info["recur_sessions"] >= 1
    assert info["useful_count"] == 0
    b.close()
    os.remove(db)


def test_harmful_feedback_is_graded(mem):
    r = mem.remember("The API base path is /v1.", subject="api")
    before = mem.inspect(memory_id=r["memory_id"])["strength"]
    # ONE harmful mark down-weights but does NOT yet flag stale or drop guardrail
    # protection — a single noisy/adversarial signal shouldn't do all three.
    mem.record_feedback([r["memory_id"]], "harmful")
    info = mem.inspect(memory_id=r["memory_id"])["memory"]
    assert info["harmful_count"] == 1 and info["stale"] is False
    assert mem.inspect(memory_id=r["memory_id"])["strength"] < before  # down-weighted
    # A SECOND harmful crosses the threshold → now flagged stale for review.
    mem.record_feedback([r["memory_id"]], "harmful")
    info2 = mem.inspect(memory_id=r["memory_id"])["memory"]
    assert info2["harmful_count"] == 2 and info2["stale"] is True


def test_useful_reverses_a_harmful_mark(mem):
    r = mem.remember("The frontend is React + Vite.", subject="stack")
    mem.record_feedback([r["memory_id"]], "harmful")
    mem.record_feedback([r["memory_id"]], "useful")  # reverses the (wrong) downvote
    info = mem.inspect(memory_id=r["memory_id"])["memory"]
    assert info["useful_count"] == 1 and info["harmful_count"] == 0


def test_useful_feedback_reinforces(mem):
    r = mem.remember("The frontend is React + Vite.", subject="stack")
    mem.record_feedback([r["memory_id"]], "useful")
    assert mem.inspect(memory_id=r["memory_id"])["memory"]["useful_count"] == 1


def test_heavy_intrasession_use_is_not_mislabeled_noise(make_mem):
    """v1.3 flywheel fix: a memory injected many times *this session* is reinforced
    (strong), so it is NOT mistaken for noise and quarantined — the old false
    positive. Pruning is decay-gated; a strong memory survives."""
    m = make_mem(Config())
    r = m.remember("Filler note that keeps matching a recurring query.", subject="filler")
    for _ in range(6):  # injected repeatedly in one session → reinforced, not noise
        m.recall("filler note recurring query")
    m.compact()
    assert m.inspect(memory_id=r["memory_id"])["status"] == "active"


def test_cold_memory_is_decay_pruned_safely(make_mem, clock):
    """A genuinely cold memory (decayed below the floor, never reinforced) is
    pruned by compaction, while a reinforced one survives — and the guardrail
    does not roll back, because pruning the cold one doesn't hurt recall."""
    m = make_mem(Config())
    cold = m.remember("An old note nobody queries anymore.", subject="cold")
    keep = m.remember("The database is Postgres.", subject="db")
    clock.advance_days(120)                 # both decay well below the floor
    m.recall("what database do we use")     # reinforce 'keep' back above the floor
    res = m.compact()
    assert m.inspect(memory_id=cold["memory_id"])["status"] in ("quarantined", "deleted")
    assert m.inspect(memory_id=keep["memory_id"])["status"] == "active"
    assert res["guardrail"]["rolled_back"] is False  # pruning noise didn't hurt real recall


def test_health_score_and_drift(mem):
    for i in range(4):
        mem.remember(f"Fact {i} about topic.", subject=f"t{i}")
    h = mem.health()
    assert 0 <= h["score"] <= 100 and h["grade"] in ("A", "B", "C", "D")
    # Flag a stale one → stale_rate rises.
    mem.remember("Anchored fact.", subject="z", source_ref="spec:x.md")
    mem.flag_stale("spec:x.md")
    assert mem.health()["stale_rate"] > 0


def test_diet_report_has_health_block(mem):
    mem.remember("x", subject="a")
    assert "health" in mem.diet_report("all")


# --- auto mistake-capture via the PostToolUse hook (subprocess) ---
def _run_hook(event, db, stdin_obj):
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    subprocess.run([sys.executable, "-m", "leptin", "hook", event, "--db", db],
                   input=json.dumps(stdin_obj), capture_output=True, text=True, env=env)


def test_post_tool_use_failure_captures_lesson(tmp_path):
    db = str(tmp_path / "loop.db")
    _run_hook("post-tool-use", db, {
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"},
        "tool_response": {"is_error": True, "error": "Operation not permitted"},
    })
    from leptin.api import Leptin
    with Leptin(db) as m:
        lessons = m.lessons()
        assert any("Bash" in lz["content"] and "Operation not permitted" in lz["content"]
                   for lz in lessons)


def test_post_tool_use_success_captures_nothing(tmp_path):
    db = str(tmp_path / "loop2.db")
    _run_hook("post-tool-use", db, {
        "hook_event_name": "PostToolUse", "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"is_error": False, "output": "ok"},
    })
    from leptin.api import Leptin
    with Leptin(db) as m:
        assert m.lessons() == []
