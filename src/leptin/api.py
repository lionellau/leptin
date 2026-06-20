"""High-level facade: open a store, get a ready-to-use engine.

    from leptin.api import Leptin
    mem = Leptin("~/.leptin/memory.db")
    mem.remember("The user prefers dark mode.", subject="prefs")
    print(mem.recall("what theme does the user want?"))
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

from leptin.config import Config
from leptin.engine import DietEngine
from leptin.storage import Store


def _expand(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


class Leptin:
    def __init__(
        self,
        db_path: str = ":memory:",
        config: Optional[Config] = None,
        session_id: Optional[str] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        path = db_path if db_path == ":memory:" else _expand(db_path)
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.store = Store(path, clock=clock)

        if config is None:
            persisted = self.store.load_config()
            if persisted:
                config = Config.from_dict({**Config().to_dict(), **persisted})
            else:
                config = Config.from_env()
        self.config = config

        # Persist a session id so separate CLI invocations within a short window
        # (`leptin remember …` then `leptin report --window session`) share one
        # session. The MCP server holds one process, so it gets a stable id too.
        if session_id is None and path != ":memory:":
            session_id = self._resume_or_new_session()
        self.engine = DietEngine(self.store, config, session_id=session_id)

    _SESSION_WINDOW_S = 1800.0  # 30 min of inactivity ends a CLI "session"

    def _resume_or_new_session(self) -> str:
        import uuid

        state = self.store.load_config()
        now = self.store.now()
        last_id = state.get("_session_id")
        last_ts = state.get("_session_ts")
        if last_id and isinstance(last_ts, (int, float)) and (now - last_ts) < self._SESSION_WINDOW_S:
            sid = str(last_id)
        else:
            sid = uuid.uuid4().hex
        self.store.save_config({"_session_id": sid, "_session_ts": now})
        return sid

    # --- persistence of config ---
    def save_config(self) -> None:
        self.store.save_config(self.config.to_dict())

    # --- tool surface (delegates) ---
    def remember(self, content: str, subject: Optional[str] = None,
                 source: Optional[str] = None, mtype: str = "fact",
                 source_ref: Optional[str] = None) -> dict[str, Any]:
        return self.engine.remember(content, subject=subject, source=source,
                                    mtype=mtype, source_ref=source_ref)

    def remember_lesson(self, content: str, subject: Optional[str] = None,
                        source: Optional[str] = None) -> dict[str, Any]:
        """Store a never-decaying lesson / anti-pattern (proactively re-injected)."""
        return self.engine.remember(content, subject=subject, source=source,
                                    mtype="lesson")

    def recall(self, query: str, token_budget: Optional[int] = None,
               k: Optional[int] = None) -> dict[str, Any]:
        return self.engine.recall(query, token_budget=token_budget, k=k)

    def session_context(self, query: Optional[str] = None,
                        token_budget: Optional[int] = None) -> dict[str, Any]:
        return self.engine.session_context(query=query, token_budget=token_budget)

    def lessons(self) -> list[dict[str, Any]]:
        return self.engine.lessons()

    def flag_stale(self, source_ref: str) -> dict[str, Any]:
        return self.engine.flag_stale(source_ref)

    def record_feedback(self, memory_ids: list[str], signal: str) -> dict[str, Any]:
        return self.engine.record_feedback(memory_ids, signal)

    def capture_lesson(self, content: str, subject: str = "anti-pattern") -> dict[str, Any]:
        return self.engine.capture_lesson(content, subject=subject)

    def health(self) -> dict[str, Any]:
        return self.engine.health()

    def conflicts(self) -> list[dict[str, Any]]:
        """Same-subject memories flagged as possible (unresolved) contradictions."""
        return self.engine.conflicts()

    def superseded(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recently superseded memories + what replaced them and why (review surface)."""
        return self.engine.superseded(limit)

    def reembed(self) -> dict[str, Any]:
        """Re-embed active memories with the current embedder (recover from a past
        hosted→local downgrade)."""
        return self.engine.reembed()

    def compact(self, dry_run: bool = False) -> dict[str, Any]:
        return self.engine.compact(dry_run=dry_run)

    def forget(self, memory_id: Optional[str] = None,
               query: Optional[str] = None) -> dict[str, Any]:
        return self.engine.forget(memory_id=memory_id, query=query)

    def restore(self, memory_id: str) -> dict[str, Any]:
        return self.engine.restore(memory_id)

    def inspect(self, memory_id: Optional[str] = None,
                query: Optional[str] = None) -> dict[str, Any]:
        return self.engine.inspect(memory_id=memory_id, query=query)

    def diet_report(self, window: str = "session") -> dict[str, Any]:
        return self.engine.diet_report(window=window)

    # --- self-tuning (v0.2) ---
    def tune(self, dry_run: bool = False) -> dict[str, Any]:
        result = self.engine.tuner.tune(dry_run=dry_run, trigger="manual")
        if result.get("accepted") and not dry_run:
            self.config = self.engine.config  # adopt the tuned config
        return result

    def tune_rollback(self, version: Optional[int] = None) -> dict[str, Any]:
        result = self.engine.tuner.rollback(version=version)
        if result.get("rolled_back"):
            self.config = self.engine.config
        return result

    def tune_history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.engine.tuner.history(limit)

    # --- guardrail probe management ---
    def add_probe(self, question: str, expected_fact: str) -> str:
        return self.engine.add_probe(question, expected_fact)

    def list_probes(self) -> list[dict[str, Any]]:
        return self.store.list_probes()

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Leptin":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
