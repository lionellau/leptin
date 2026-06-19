"""Real-world quality: migrations, concurrency, scale, doctor, dataset, logging."""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from leptin import bench
from leptin.config import Config
from leptin.storage import SCHEMA_VERSION, Store

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "locomo_mini.json")


# --------------------------------------------------------------- migrations
def test_fresh_db_is_at_latest_schema_version(tmp_path):
    s = Store(str(tmp_path / "fresh.db"))
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()


def test_old_db_is_migrated_up(tmp_path):
    """An older DB (missing a recent column + tune tables) is upgraded on open."""
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY, subject TEXT, content TEXT, embedding TEXT,
            tokens INTEGER, strength REAL, created_at REAL, last_accessed_at REAL,
            access_count INTEGER, status TEXT, source_session TEXT
        );
        PRAGMA user_version=0;
        """
    )
    raw.execute("INSERT INTO memories (id, content, tokens, strength, created_at,"
                " last_accessed_at, access_count, status) VALUES "
                "('m1','old fact',2,1.0,1.0,1.0,0,'active')")
    raw.commit()
    raw.close()

    s = Store(path)  # opening runs migrations
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(memories)").fetchall()}
    assert {"reversible_until", "superseded_by", "provenance"} <= cols  # added by migration
    # The new self-tuning tables exist too, and old data survived.
    assert s.count_memories("active") == 1
    s.conn.execute("SELECT 1 FROM config_versions LIMIT 1")  # table exists (no error)
    s.close()


# --------------------------------------------------------------- concurrency
def test_concurrent_writers_do_not_lock_error(tmp_path):
    """busy_timeout lets multiple connections to the same file coexist."""
    path = str(tmp_path / "concurrent.db")
    Store(path).close()  # create schema

    errors = []

    def writer(n):
        try:
            s = Store(path)
            for i in range(25):
                s.add_memory(f"fact {n}-{i}", [0.1, 0.2], tokens=2, subject=f"s{n}")
            s.close()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent writes errored: {errors}"
    assert Store(path).count_memories("active") == 100


# --------------------------------------------------------------- scale + cache
def test_embedding_cache_avoids_reparse(tmp_path):
    s = Store(str(tmp_path / "cache.db"))
    m = s.add_memory("cached fact", [1.0, 2.0, 3.0], tokens=2)
    assert s._emb_cache[m["id"]] == [1.0, 2.0, 3.0]
    s.list_memories("active")  # hits cache, no re-parse
    # Updating the embedding invalidates the cache entry.
    s.update_memory(m["id"], embedding=[9.0])
    assert m["id"] not in s._emb_cache
    assert s.get_memory(m["id"])["embedding"] == [9.0]
    s.close()


@pytest.mark.parametrize("n", [2000])
def test_recall_latency_at_scale(n):
    """Recall stays fast at a few thousand memories (embedding cache working)."""
    from leptin.engine import DietEngine

    store = Store(":memory:")
    eng = DietEngine(store, Config())
    for i in range(n):
        eng.remember(f"memory number {i} about topic {i % 50} and some detail", subject=f"t{i % 50}")
    eng.recall("topic 7 detail")  # warm the cache
    t0 = time.perf_counter()
    for _ in range(5):
        eng.recall("topic 13 detail")
    avg_ms = (time.perf_counter() - t0) / 5 * 1000
    assert avg_ms < 500, f"recall too slow at {n} memories: {avg_ms:.0f}ms"
    store.close()


# --------------------------------------------------------------- LoCoMo loader
def test_locomo_loader_parses_fixture():
    corpus = bench.load_locomo(FIXTURE)
    assert len(corpus["inserts"]) == 6  # 3 turns × 2 sessions
    questions = [q for q, _ in corpus["probes"]]
    assert any("dog" in q for q in questions)
    subjects = {subj for subj, _ in corpus["inserts"]}
    assert "Alice" in subjects


def test_bench_runs_on_locomo_fixture():
    r = bench.run(corpus=bench.load_locomo(FIXTURE), budget=500)
    assert "token_reduction_pct" in r and "leptin_recall" in r


def test_locomo_loader_rejects_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    with pytest.raises(ValueError):
        bench.load_locomo(str(bad))


# --------------------------------------------------------------- doctor (CLI)
def test_doctor_reports_healthy(tmp_path, capsys):
    from leptin.cli import main

    db = str(tmp_path / "doc.db")
    rc = main(["doctor", "--db", db])
    out = capsys.readouterr().out
    assert "Leptin doctor" in out
    assert "Embeddings" in out and "Self-tuning" in out
    assert rc == 0  # offline default is healthy


def test_doctor_warns_on_hosted_without_sdk(tmp_path, capsys, monkeypatch):
    from leptin.api import Leptin
    from leptin.cli import main

    db = str(tmp_path / "doc2.db")
    Leptin(db, Config(embedding_model="text-embedding-3-small")).save_config()
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    main(["doctor", "--db", db])
    out = capsys.readouterr().out
    assert "Embeddings" in out
    # Either the SDK is missing or the key is unset → a warning line is present.
    assert "⚠" in out


# --------------------------------------------------------------- logging
def test_log_level_from_env(monkeypatch):
    import importlib

    import leptin.logconf as lc
    monkeypatch.setenv("LEPTIN_LOG", "DEBUG")
    importlib.reload(lc)
    logger = lc.get_logger()
    assert logger.level == 10  # DEBUG
    monkeypatch.setenv("LEPTIN_LOG", "WARNING")
    importlib.reload(lc)
    assert lc.get_logger().level == 30  # WARNING
