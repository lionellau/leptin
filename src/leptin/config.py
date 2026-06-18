"""Configuration for a Leptin store.

Every tunable lives here with a sane default so Leptin works with zero setup.
Values can be overridden per-store (constructor), persisted in the ``config``
table, or supplied via environment variables (``LEPTIN_*``).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any


# Per-model token prices in USD per 1,000,000 tokens (input, output).
# Memory injection is an *input* cost, so savings are valued at the input price.
# These are illustrative defaults; override via ``price_table`` for accuracy.
DEFAULT_PRICE_TABLE: dict[str, dict[str, float]] = {
    "claude-opus-4-8": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0},
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    # Fallback used when the configured model isn't in the table.
    "default": {"input": 3.0, "output": 15.0},
}


@dataclass
class Config:
    """All Leptin tunables. Construct with overrides, or use :meth:`from_env`."""

    # --- Recall budgeting ---
    token_budget_default: int = 1500
    """Hard ceiling (in tokens) on memories injected per ``recall``."""

    recall_k: int = 50
    """How many candidate memories to consider before packing."""

    naive_top_k: int = 10
    """The baseline a naive store would dump into context (for savings math)."""

    recall_rel_floor: float = 0.55
    """Inject only memories at least this fraction as similar as the best match.

    This is the core of *packed* recall: a naive store dumps a fixed top-k
    (including marginally-relevant padding); Leptin injects only what's actually
    on-topic, under the budget. Set to 0.0 to recover naive top-k behaviour."""

    recall_min_sim: float = 0.0
    """Absolute similarity floor below which a memory is never injected."""

    # --- Dedup / merge ---
    dedup_threshold: float = 0.86
    """Cosine similarity τ above which two memories are near-duplicates."""

    contradiction_threshold: float = 0.5
    """Below this similarity a same-subject conflict is treated as supersede."""

    # --- Decay / forgetting ---
    decay_half_life_days: float = 14.0
    """Days for an un-accessed memory's strength to halve (Ebbinghaus-style)."""

    strength_floor: float = 0.15
    """Memories whose effective strength drops below this are prune-eligible."""

    access_boost: float = 0.4
    """Strength added (capped at 1.0) each time a memory is recalled."""

    # --- Recall guardrail ---
    guardrail_max_drop: float = 0.02
    """Max tolerated recall drop (fraction) before a compaction is rolled back."""

    max_probes: int = 40
    """Cap on auto-derived probes used by the guardrail."""

    # --- Reversibility ---
    reversible_window_days: float = 30.0
    """How long quarantined/forgotten memories remain restorable."""

    # --- Models / pricing ---
    embedding_model: str = "local-hash"
    """``local-hash`` (offline), or e.g. ``text-embedding-3-small`` / ``voyage-3``."""

    llm_model: str = "heuristic"
    """``heuristic`` (offline merge), or e.g. ``claude-haiku-4-5`` / ``gpt-4o-mini``."""

    price_model: str = "claude-sonnet-4-6"
    """Which price_table entry to value savings against."""

    price_table: dict[str, dict[str, float]] = field(
        default_factory=lambda: dict(DEFAULT_PRICE_TABLE)
    )

    # --- Backend ---
    backend: str = "sqlite"
    """Storage backend: ``sqlite`` (v1). ``mem0``/``pgvector`` are P1/P2 adapters."""

    embedding_dim: int = 256
    """Dimensionality of the local hashing embedder."""

    def input_price_per_token(self) -> float:
        entry = self.price_table.get(self.price_model) or self.price_table.get(
            "default", {"input": 3.0}
        )
        return float(entry.get("input", 3.0)) / 1_000_000.0

    def usd_for_tokens(self, tokens: int) -> float:
        return max(0, tokens) * self.input_price_per_token()

    # --- (de)serialization helpers ---
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Config":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_env(cls, **overrides: Any) -> "Config":
        """Build a Config, layering env vars (``LEPTIN_*``) then explicit overrides.

        Coercion is driven by each field's annotation, so a new tunable can never
        silently arrive as a string (and crash recall, as a hand-maintained
        whitelist once allowed). Invalid values are skipped, not stored raw.
        """
        env: dict[str, Any] = {}
        for f in fields(cls):
            raw = os.environ.get(f"LEPTIN_{f.name.upper()}")
            if raw is None:
                continue
            t = str(f.type)
            try:
                if t.startswith("bool"):
                    env[f.name] = raw.strip().lower() in ("1", "true", "yes", "on")
                elif t.startswith("int"):
                    env[f.name] = int(raw)
                elif t.startswith("float"):
                    env[f.name] = float(raw)
                elif t.startswith(("dict", "list")):
                    env[f.name] = json.loads(raw)
                else:
                    env[f.name] = raw
            except (ValueError, json.JSONDecodeError):
                continue  # ignore malformed env override; keep the default
        env.update(overrides)
        return cls.from_dict(env)
