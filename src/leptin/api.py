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
        self.engine = DietEngine(self.store, config, session_id=session_id)

    # --- persistence of config ---
    def save_config(self) -> None:
        self.store.save_config(self.config.to_dict())

    # --- tool surface (delegates) ---
    def remember(self, content: str, subject: Optional[str] = None,
                 source: Optional[str] = None) -> dict[str, Any]:
        return self.engine.remember(content, subject=subject, source=source)

    def recall(self, query: str, token_budget: Optional[int] = None,
               k: Optional[int] = None) -> dict[str, Any]:
        return self.engine.recall(query, token_budget=token_budget, k=k)

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
