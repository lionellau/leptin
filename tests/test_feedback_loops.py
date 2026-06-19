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


def test_recurrence_across_sessions_marks_useful(make_mem, clock):
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
    b.recall("where are prod secrets?")     # needed again in a later session → useful
    assert b.inspect(memory_id=r["memory_id"])["memory"]["useful_count"] >= 1
    b.close()
    os.remove(db)


def test_harmful_feedback_downweights(mem):
    r = mem.remember("The API base path is /v1.", subject="api")
    before = mem.inspect(memory_id=r["memory_id"])["strength"]
    out = mem.record_feedback([r["memory_id"]], "harmful")
    assert out["count"] == 1
    info = mem.inspect(memory_id=r["memory_id"])["memory"]
    assert info["harmful_count"] == 1 and info["stale"] is True
    assert mem.inspect(memory_id=r["memory_id"])["strength"] < before  # down-weighted


def test_useful_feedback_reinforces(mem):
    r = mem.remember("The frontend is React + Vite.", subject="stack")
    mem.record_feedback([r["memory_id"]], "useful")
    assert mem.inspect(memory_id=r["memory_id"])["memory"]["useful_count"] == 1


def test_noise_memory_is_pruned_by_compaction(make_mem):
    """A memory injected many times that never proves useful is safely pruned;
    the recall-usefulness loop's prune signal, guardrailed."""
    m = make_mem(Config())
    noise = m.remember("Filler note that matches a recurring query but never helps.",
                       subject="filler")
    # An unrelated, genuinely-used memory that must survive.
    keep = m.remember("The database is Postgres.", subject="db")
    for _ in range(6):  # inject the noise repeatedly (> _NOISE_INJECTS), same session
        m.recall("filler note recurring query")
    res = m.compact()
    assert m.inspect(memory_id=noise["memory_id"])["status"] in ("quarantined", "deleted")
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
