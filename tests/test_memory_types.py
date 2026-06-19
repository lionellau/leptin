"""v1.1 — memory typing, never-decaying lessons, provenance anchoring, hooks."""

from __future__ import annotations

import json
import os
import subprocess
import sys

from leptin.config import Config


def test_lessons_never_decay(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    fact = m.remember("Standup is at 9:30am.", subject="x")              # fact
    lesson = m.remember("Never run migrations on Friday.", mtype="lesson")  # lesson
    clock.advance_days(120)  # long enough that a fact decays well below the floor
    fact_s = m.inspect(memory_id=fact["memory_id"])["strength"]
    lesson_s = m.inspect(memory_id=lesson["memory_id"])["strength"]
    assert fact_s < 0.15        # the fact decayed
    assert lesson_s >= 0.99     # the lesson did not


def test_lesson_survives_compaction(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    les = m.remember("Anti-pattern: don't cache user PII.", mtype="lesson")
    m.remember("An ordinary fact that will decay.", subject="y")
    clock.advance_days(120)
    res = m.compact()
    # The decayed fact may be pruned; the lesson must remain active.
    assert m.inspect(memory_id=les["memory_id"])["status"] == "active"


def test_task_decays_faster_than_fact(make_mem, clock):
    m = make_mem(Config(decay_half_life_days=10.0))
    fact = m.remember("A durable fact.", subject="a")
    task = m.remember("Tied to ticket ABC-1.", subject="b", mtype="task")
    clock.advance_days(10)
    assert (m.inspect(memory_id=task["memory_id"])["strength"]
            < m.inspect(memory_id=fact["memory_id"])["strength"])


def test_provenance_anchor_and_stale(mem):
    r = mem.remember("Auth uses JWT in cookies.", subject="auth",
                     source_ref="spec:auth.md#tokens")
    assert mem.inspect(memory_id=r["memory_id"])["memory"]["source_ref"] == "spec:auth.md#tokens"
    out = mem.flag_stale("spec:auth.md#tokens")
    assert out["count"] == 1
    assert mem.inspect(memory_id=r["memory_id"])["memory"]["stale"] is True
    # Stale memory is down-weighted but not hidden.
    res = mem.recall("how does auth work?")
    assert any("JWT" in mm["content"] for mm in res["memories"])


def test_session_context_includes_lessons(mem):
    mem.remember("Never force-push to main.", mtype="lesson")
    mem.remember("The project database is Postgres.", subject="stack")
    ctx = mem.session_context(query="what project database is used")
    # Lessons are ALWAYS injected at session start, regardless of query.
    assert any("force-push" in lz["content"] for lz in ctx["lessons"])
    assert "Lessons learned" in ctx["text"]
    assert "force-push" in ctx["text"]
    # Query-relevant memory is injected alongside.
    assert any("Postgres" in mm["content"] for mm in ctx["memories"])


def test_session_context_lessons_without_query(mem):
    mem.remember("Always pin dependencies.", mtype="lesson")
    ctx = mem.session_context()  # no query → still injects lessons
    assert any("pin dependencies" in lz["content"] for lz in ctx["lessons"])


def test_lessons_listing(mem):
    mem.remember("Lesson one.", mtype="lesson")
    mem.remember("A fact.", subject="x")
    lessons = mem.lessons()
    assert len(lessons) == 1 and lessons[0]["mtype"] == "lesson"


# --- hook entrypoint (subprocess, like the host would invoke it) ---
SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


def _run_hook(event, db, stdin_obj):
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run([sys.executable, "-m", "leptin", "hook", event, "--db", db],
                       input=json.dumps(stdin_obj), capture_output=True, text=True, env=env)
    return p.stdout


def test_hook_session_start_injects_lessons(tmp_path):
    db = str(tmp_path / "hook.db")
    from leptin.api import Leptin
    with Leptin(db) as m:
        m.remember("Always write a test first.", mtype="lesson")
    out = _run_hook("session-start", db, {"hook_event_name": "SessionStart"})
    payload = json.loads(out)
    ctx = payload["hookSpecificOutput"]["additionalContext"]
    assert "Always write a test first." in ctx
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_hook_never_throws_on_bad_input(tmp_path):
    db = str(tmp_path / "hook2.db")
    # Empty store + garbage stdin must still exit cleanly (a hook can't break the host).
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    p = subprocess.run([sys.executable, "-m", "leptin", "hook", "session-start", "--db", db],
                       input="not json", capture_output=True, text=True, env=env)
    assert p.returncode == 0
