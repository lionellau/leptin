"""v1.3 — the credibility release: graded contradiction detection, a non-circular
guardrail, budgeted session-start injection, reversible/discoverable supersede,
the reframed flywheel, embedder provenance, bounded lessons, and tuner determinism.
These encode the corrected behaviour the adversarial review demanded."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings

from leptin.config import Config
from leptin.llm import contradiction_signal

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


# --- graded contradiction detector (the flagship fix) -----------------------
def test_value_swap_is_certain():
    assert contradiction_signal("we use pnpm", "we use bun").certain
    assert contradiction_signal("the deploy region is us-east-1",
                                "the deploy region is us-west-2").certain
    assert contradiction_signal("the user prefers dark mode",
                                "the user prefers light mode").certain


def test_numeric_reversal_certain_but_numeric_different_fact_is_not():
    assert contradiction_signal("the free trial is 14 days",
                                "the free trial is 30 days").certain
    # the false-positive that buried a true fact: different facts that merely
    # both contain numbers must NOT auto-supersede.
    sig = contradiction_signal("the box has 8 cpu cores", "the box has 32 gb ram")
    assert not sig.certain


def test_hard_paraphrase_is_uncertain_not_certain():
    sig = contradiction_signal("Auth uses JWT in cookies.",
                               "Auth uses session tokens in headers.")
    assert not sig.certain and sig.uncertain  # flagged for review, not buried


def test_unrelated_same_subject_not_flagged():
    assert not contradiction_signal("the backend is FastAPI",
                                    "the frontend is React").certain
    assert not contradiction_signal("the user prefers dark mode",
                                    "the user prefers spaces over tabs").certain


def test_uncertain_conflict_is_flagged_not_buried(mem):
    # A multi-token divergence that's lexically similar (so the offline embedder
    # links them) but not confidently mutually-exclusive → flag, don't bury.
    a = mem.remember("We deploy to production every Friday afternoon.", subject="deploy")
    b = mem.remember("We deploy to staging every Monday afternoon.", subject="deploy")
    assert b["action"] == "created"  # uncertain → both kept, nothing buried
    active = {x["id"] for x in mem.engine.store.list_memories("active")}
    assert {a["memory_id"], b["memory_id"]} <= active
    assert mem.health()["conflicts"] >= 1
    assert mem.conflicts()  # surfaced for review


# --- non-circular guardrail (verify the fact, not just the id) --------------
def test_guardrail_verifies_fact_not_just_id(mem):
    r = mem.remember("The deploy region is us-west-2.", subject="infra")
    probes = [{"question": "deploy region", "expected_fact": "us-west-2",
               "source_memory_id": r["memory_id"]}]
    assert mem.engine.guardrail.measure(probes) == 1.0
    # Keep the id, drop the value (the merge-drop bug the id-only check missed).
    mem.store.update_memory(r["memory_id"], content="The deploy region is unspecified.",
                            embedding=mem.engine._embed("The deploy region is unspecified."))
    assert mem.engine.guardrail.measure(probes) < 1.0


def test_compact_report_has_probe_confidence(mem):
    mem.remember("The database is Postgres.", subject="db")
    rep = mem.compact()
    g = rep["guardrail"]
    assert "low_confidence" in g and "verbatim_probe_fraction" in g
    assert g["low_confidence"] is True  # offline = lexical embedder


# --- budgeted session-start injection ---------------------------------------
def test_session_context_respects_budget(make_mem):
    m = make_mem(Config())
    for i in range(20):
        m.remember_lesson(f"Lesson {i}: avoid mistake {i} in subsystem number {i} of the app.",
                          subject=f"l{i}")
    ctx = m.session_context(token_budget=60)
    assert ctx["tokens"] <= 60               # the WHOLE payload respects the budget
    assert ctx["lessons_omitted"] > 0
    assert "more lessons" in ctx["text"]


def test_session_context_feeds_the_flywheel(make_mem):
    m = make_mem(Config())
    r = m.remember_lesson("Never force-push to main.")
    m.session_context()  # the push path must count as an injection
    assert m.inspect(memory_id=r["memory_id"])["memory"]["inject_count"] >= 1


# --- reversible + discoverable supersede ------------------------------------
def test_supersede_is_reversible_and_discoverable(mem):
    mem.remember("The trial is 14 days.", subject="billing")
    mem.remember("The trial is 30 days.", subject="billing")
    sup = mem.superseded()
    old = next((s for s in sup if "14 days" in s["memory"]["content"]), None)
    assert old is not None
    assert old["reversible_until"] is not None        # time-boxed, not a silent drop
    assert mem.restore(old["memory"]["memory_id"])["restored"] is True


# --- embedder provenance + recovery -----------------------------------------
def test_embedder_provenance_and_reembed(mem):
    r = mem.remember("The cache layer is Redis.", subject="infra")
    info = mem.inspect(memory_id=r["memory_id"])["memory"]
    assert info["embedder"] and info["embedder"].startswith("local-hash:")
    out = mem.reembed()
    assert out["reembedded"] >= 1


# --- bounded, demotable lessons ---------------------------------------------
def test_candidate_lesson_decays_but_handauthored_does_not(make_mem, clock):
    m = make_mem(Config())
    auto = m.engine.capture_lesson("Avoid: Bash (npm ci) failed — lockfile out of sync")
    hand = m.remember_lesson("Never run DB migrations on a Friday.")
    clock.advance_days(90)
    auto_s = m.inspect(memory_id=auto["memory_id"])["strength"]
    hand_s = m.inspect(memory_id=hand["memory_id"])["strength"]
    assert hand_s == 1.0           # hand-authored lessons never decay
    assert auto_s < hand_s         # un-graduated auto candidate decays


def test_auto_lesson_corpus_is_capped(make_mem):
    m = make_mem(Config(max_auto_lessons=3))
    for i in range(7):
        m.engine.capture_lesson(f"Avoid: tool{i} failed — reason {i}", subject=f"ap{i}")
    active_auto = [x for x in m.engine.store.list_memories("active")
                  if x.get("provenance") == "auto-captured"]
    assert len(active_auto) <= 3


# --- scale-quick-tier + integrity -------------------------------------------
def test_emb_cache_is_lru_bounded():
    from leptin.storage import Store, _EMB_CACHE_MAX

    s = Store(":memory:")
    for i in range(_EMB_CACHE_MAX + 50):
        s.add_memory(content=f"m{i}", embedding=[float(i)], tokens=1)
    assert len(s._emb_cache) <= _EMB_CACHE_MAX
    s.close()


def test_subjectless_dedup_is_null_safe(mem):
    mem.remember("The build uses Bazel for everything in the monorepo.")  # subject None
    r = mem.remember("The build uses Bazel for everything in the monorepo.")  # exact dup
    assert r["action"] in ("merged", "superseded")
    same = [x for x in mem.engine.store.list_memories("active") if "Bazel" in x["content"]]
    assert len(same) == 1


def test_backend_not_yet_wired_warns_and_falls_back():
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cfg = Config(backend="mem0")
    assert cfg.backend == "sqlite"
    assert any("not yet wired" in str(x.message) for x in w)


def test_tuner_split_is_deterministic_across_hash_seeds():
    """The held-out split must not depend on PYTHONHASHSEED (the salted builtin
    hash() silently broke the 'deterministic' contract)."""
    snippet = (
        "import sys; sys.path.insert(0, %r);"
        "from leptin.tuner import Tuner;"
        "ps=[{'question': f'q{i}', 'expected_fact': 'x'} for i in range(40)];"
        "v,h=Tuner._split_probes(ps);"
        "print(','.join(sorted(p['question'] for p in h)))" % SRC
    )
    outs = []
    for seed in ("0", "1", "12345"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        r = subprocess.run([sys.executable, "-c", snippet],
                           capture_output=True, text=True, env=env)
        outs.append(r.stdout.strip())
    assert outs[0] and len(set(outs)) == 1  # identical partition regardless of seed


def test_health_score_is_floor_free_and_normalized(mem):
    # A fully-stale store should map toward 0, not underflow past it.
    for i in range(4):
        mem.remember(f"Fact {i} anchored to a spec.", subject=f"s{i}", source_ref=f"spec:{i}.md")
        mem.flag_stale(f"spec:{i}.md")
    h = mem.health()
    assert 0 <= h["score"] <= 100
