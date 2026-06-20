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

    rank_candidate_limit: int = 0
    """If > 0, prefilter to this many cheap keyword/subject candidates before the
    full cosine scan in `_rank` (a scale guard for large stores). 0 = scan all
    (exact, the default for correctness)."""

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

    procedural_halflife_mult: float = 2.0
    """How-to/workflow memories decay this many times *slower* than facts."""

    task_halflife_mult: float = 0.4
    """Ticket-scoped task notes decay this many times *faster* than facts.
    (Facts use 1.0 by definition; lessons never decay.)"""

    strength_floor: float = 0.15
    """Memories whose effective strength drops below this are prune-eligible."""

    access_boost: float = 0.4
    """Strength added (capped at 1.0) each time a memory is recalled."""

    stale_penalty: float = 0.25
    """Recall-score multiplier for a memory whose source_ref changed (down-weight, don't hide)."""

    harmful_penalty: float = 0.4
    """Strength multiplier applied *per* 'harmful' feedback mark."""

    forget_min_sim: float = 0.55
    """Similarity floor for query-targeted `forget` to select a memory."""

    # --- Usefulness loop / noise ---
    noise_inject_count: int = 5
    """Injected at least this many times with zero usefulness → treated as noise.
    Noise is prune-eligible ONLY when also below the strength floor (a strong,
    genuinely-used memory is never noise)."""

    recur_cooldown_seconds: float = 3600.0
    """A re-injection after at least this long counts as a 'needed it again' recurrence
    signal even within one session (recency-gated, so back-to-back recalls don't inflate it)."""

    harmful_stale_threshold: int = 2
    """'harmful' marks needed before a memory is flagged stale AND dropped from the
    guardrail's protected set. The first mark only down-weights — one noisy/adversarial
    signal shouldn't both cripple recall and blind the safety net."""

    drift_stale_rate: float = 0.25
    """Health: flag drift when the stale fraction exceeds this."""

    drift_noise_rate: float = 0.25
    """Health: flag drift when the noise fraction exceeds this."""

    # --- Lessons policy ---
    lesson_budget_frac: float = 0.5
    """Fraction of the session-start token budget reserved for lessons (the rest
    goes to query-relevant memories). Lessons are ranked + packed under this
    sub-budget so a growing lesson corpus can't blow the whole context."""

    max_auto_lessons: int = 200
    """Cap on *auto-captured* candidate lessons; the least-useful are retired
    (reversibly) past this. Hand-authored lessons are exempt and always kept."""

    candidate_lesson_half_life_days: float = 30.0
    """Auto-captured lessons decay on this half-life until they 'graduate' (recur /
    confirmed); hand-authored lessons never decay."""

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

    # --- Self-tuning (v0.2; see PRD §13). Offline-safe; opt-in. ---
    self_tune_enabled: bool = False
    """Run a self-tuning cycle automatically at the tail of compact()."""

    tune_objective: str = "balanced"
    """``balanced`` (0.5), ``savings`` (0.8), or ``recall`` (0.2) weight on savings."""

    tune_min_new_memories: int = 20
    """Auto-tune trigger: memories created since the last tune."""

    tune_max_interval_days: float = 7.0
    """Cadence sentinel: tune at least this often (catches drift with no writes)."""

    tune_recall_floor: float = 0.85
    """Auto-tune trigger: probe hit-rate below this forces a cycle."""

    tune_epsilon: float = 0.005
    """Minimum objective gain to accept a tuned config."""

    tune_replay_n: int = 200
    """Cap on recent recall queries replayed during an eval (bounds cost)."""

    tune_max_coords_per_cycle: int = 2
    """Knobs adjusted per tune cycle (bounds cost)."""

    tune_freeze_days: float = 14.0
    """Meta-guardrail: disable self-tuning this long after repeated failures."""

    tune_savings_floor: float = 0.0
    """Max tolerated drop in token-reduction before a tuned config is rejected."""

    def __post_init__(self) -> None:
        """Clamp tunables to sane ranges so a bad value can't crash or mislead.

        Cosine thresholds may exceed 1.0 to mean "never" (e.g. dedup_threshold=2
        disables merging — the benchmark relies on this), so those get a wider
        ceiling; fractions are clamped to [0, 1]; counts/durations to positive.
        """
        def clamp(v, lo, hi):
            try:
                return max(lo, min(hi, v))
            except TypeError:
                return v

        # Fractions in [0, 1]
        self.strength_floor = clamp(self.strength_floor, 0.0, 1.0)
        self.access_boost = clamp(self.access_boost, 0.0, 1.0)
        self.guardrail_max_drop = clamp(self.guardrail_max_drop, 0.0, 1.0)
        self.recall_rel_floor = clamp(self.recall_rel_floor, 0.0, 1.0)
        self.recall_min_sim = clamp(self.recall_min_sim, 0.0, 1.0)
        self.stale_penalty = clamp(self.stale_penalty, 0.0, 1.0)
        self.harmful_penalty = clamp(self.harmful_penalty, 0.0, 1.0)
        self.forget_min_sim = clamp(self.forget_min_sim, 0.0, 1.0)
        self.drift_stale_rate = clamp(self.drift_stale_rate, 0.0, 1.0)
        self.drift_noise_rate = clamp(self.drift_noise_rate, 0.0, 1.0)
        self.lesson_budget_frac = clamp(self.lesson_budget_frac, 0.0, 1.0)
        # Cosine thresholds: allow up to 2.0 to mean "disable".
        self.dedup_threshold = clamp(self.dedup_threshold, 0.0, 2.0)
        self.contradiction_threshold = clamp(self.contradiction_threshold, 0.0, 2.0)
        # Positive counts / sizes
        self.token_budget_default = max(1, int(self.token_budget_default))
        self.recall_k = max(1, int(self.recall_k))
        self.naive_top_k = max(1, int(self.naive_top_k))
        self.max_probes = max(1, int(self.max_probes))
        self.embedding_dim = max(1, int(self.embedding_dim))
        self.noise_inject_count = max(1, int(self.noise_inject_count))
        self.harmful_stale_threshold = max(1, int(self.harmful_stale_threshold))
        self.max_auto_lessons = max(1, int(self.max_auto_lessons))
        self.rank_candidate_limit = max(0, int(self.rank_candidate_limit))
        # Multipliers (positive)
        self.procedural_halflife_mult = max(0.0, float(self.procedural_halflife_mult))
        self.task_halflife_mult = max(0.0, float(self.task_halflife_mult))
        # Non-negative durations
        self.decay_half_life_days = max(0.0, float(self.decay_half_life_days))
        self.reversible_window_days = max(0.0, float(self.reversible_window_days))
        self.recur_cooldown_seconds = max(0.0, float(self.recur_cooldown_seconds))
        self.candidate_lesson_half_life_days = max(0.0, float(self.candidate_lesson_half_life_days))
        # Self-tuning knobs
        self.tune_recall_floor = clamp(self.tune_recall_floor, 0.0, 1.0)
        self.tune_epsilon = clamp(self.tune_epsilon, 0.0, 1.0)
        self.tune_savings_floor = clamp(self.tune_savings_floor, 0.0, 1.0)
        self.tune_min_new_memories = max(1, int(self.tune_min_new_memories))
        self.tune_replay_n = max(1, int(self.tune_replay_n))
        self.tune_max_coords_per_cycle = max(1, int(self.tune_max_coords_per_cycle))
        self.tune_max_interval_days = max(0.0, float(self.tune_max_interval_days))
        self.tune_freeze_days = max(0.0, float(self.tune_freeze_days))
        if self.tune_objective not in ("balanced", "savings", "recall"):
            self.tune_objective = "balanced"
        # The non-sqlite backends are not yet wired (no adapter ships in v1.x):
        # the loop currently governs Leptin's own SQLite. Warn rather than
        # silently pretend a `backend=mem0` switch does anything.
        if self.backend != "sqlite":
            import warnings

            warnings.warn(
                f"backend={self.backend!r} is not yet wired — Leptin currently runs its "
                f"control loop over its own SQLite store. Falling back to 'sqlite'. "
                f"(External-store adapters are on the roadmap.)",
                stacklevel=2,
            )
            self.backend = "sqlite"

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
