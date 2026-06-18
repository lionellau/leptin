"""Token counting.

Uses ``tiktoken`` when installed (and a real model is configured), otherwise a
deterministic heuristic (~4 characters per token) so the ledger math is stable
and reproducible offline.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=8)
def _tiktoken_encoder(model: str):  # pragma: no cover - exercised only with extra
    import tiktoken

    try:
        return tiktoken.encoding_for_model(model)
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str, model: str = "heuristic") -> int:
    """Return an integer token count for ``text``.

    The heuristic (``ceil(len/4)``, min 1 for non-empty) is intentionally simple
    and deterministic — the savings ledger compares *baseline vs actual* using
    the same function, so any consistent estimator yields correct deltas.
    """
    if not text:
        return 0
    if model and model not in ("heuristic", "local-hash"):
        try:
            return len(_tiktoken_encoder(model).encode(text))
        except Exception:
            pass
    return max(1, -(-len(text) // 4))  # ceil division


def serialize_memories(memories: list[dict[str, Any]]) -> str:
    """Canonical serialization of injected memories, for counting recall tokens.

    Mirrors how an agent would actually receive recalled memories: one bullet
    per memory. Counting tokens on *this* string is what the budget bounds.
    """
    lines = []
    for m in memories:
        subj = m.get("subject")
        prefix = f"[{subj}] " if subj else ""
        lines.append(f"- {prefix}{m['content']}")
    return "\n".join(lines)


def count_memory_tokens(memories: list[dict[str, Any]], model: str = "heuristic") -> int:
    return count_tokens(serialize_memories(memories), model)


def _json_tokens(obj: Any, model: str = "heuristic") -> int:
    return count_tokens(json.dumps(obj, ensure_ascii=False), model)
