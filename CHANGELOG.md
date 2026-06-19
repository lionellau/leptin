# Changelog

All notable changes to Leptin are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-06-19

### Added — 🧬 Self-tuning (closed-loop self-evolution; PRD §13)
- New `self_tune` MCP tool and `leptin tune [--dry-run] [--rollback [V]] [--history]`.
- `leptin.tuner`: a deterministic, **offline, zero-LLM-cost** control loop — replays
  the store under candidate configs, accepts a change only on a held-out, dual-metric
  (recall AND savings) win, else leaves the config untouched.
- UCB coordinate-ascent over a clamped set of recall/decay knobs; **locked** safety
  rails (the guardrail and model/price fields can never be tuned).
- **Evolution ledger** (`config_versions`, `tune_runs`) with exact `--rollback`;
  shadow-window/meta-guardrail freezes the *automatic* loop after repeated failures.
- `diet_report` gains a `tuning` block; dashboard gains a self-tuning panel + the
  evolution ledger, an `/api/inspect` route, and `/api/tuning`.
- Auto-tuning is opt-in (`self_tune_enabled`, default off); manual tune always works.

### Fixed (PRD-conformance audit — 7 P0 + P1/P2)
- `recall(token_budget=0)` now injects nothing (was the falsy-zero default bug).
- Hosted **merger** now degrades gracefully (heuristic fallback) instead of throwing
  when the LLM/SDK is unavailable on a near-duplicate (mirrors the embedder path);
  one-time stderr warning on any hosted→local downgrade.
- `compact` now also **merges/supersedes** leftover same-subject duplicates and writes
  a ledger row on every (non-dry-run) call incl. no-op/rollback (guardrail result in detail).
- Session id persists across CLI invocations, so `report --window session` works.
- `Config` clamps out-of-range values; env coercion already annotation-driven.
- Guardrail: lazy probe re-resolution + stricter unlinked-probe coverage.
- `voyageai` added to the `[hosted]` extra; expired quarantines purged on compact.
- `leptin init` prints a launchable command path; README receipts/counts refreshed.
- OSS hygiene: `SECURITY.md`, issue/PR templates. Tests: 56 → 99.

## [0.1.0] — 2026-06-18

Initial release.

### Added
- **MCP server (stdio)** exposing 7 tools: `remember`, `recall`, `compact`,
  `forget`, `restore`, `inspect`, `diet_report`. Dependency-free JSON-RPC 2.0.
- **Diet engine** — write-time dedup/merge, contradiction supersede (older kept,
  not deleted), Ebbinghaus-style time-decay with access boosting, and
  budgeted/packed recall with a relevance gate.
- **Savings ledger** — headline savings = recall *injection* savings (real,
  ongoing, never double-counted); one-time/reversible *footprint* reductions
  (merge/supersede/decay/forget) reported separately as
  `footprint_tokens_reduced`. Configurable per-model price table; `diet_report`
  aggregation by window.
- **Recall guardrail** — auto-derived + user-supplied probe sets; transactional
  compaction that auto-rolls-back any prune that would hurt recall. Coverage is
  checked by memory *identity/lineage* (and measured against exactly what
  `recall` would inject), so an unrelated survivor sharing a token can't mask a
  real loss. Expired quarantines are purged past the retention window.
- **Glass-box reversibility** — quarantine-first `forget`, `restore`, and full
  per-memory event history via `inspect`.
- **SQLite storage** (zero infra) with embeddings stored as JSON; pure-Python
  cosine scoring.
- **Local dashboard** — dependency-free HTTP server + embedded single-file UI:
  savings chart, glass-box memory browser, compaction/guardrail history.
- **Reproducible benchmark** (`leptin bench`) on a bundled, deterministic
  LoCoMo-style corpus: **66.2% token reduction at 0% recall loss**, offline.
- **CLI**: `serve`, `bench`, `init`, `report`, `remember`, `recall`, `compact`,
  `inspect`, `dashboard`.
- **56 tests** covering the PRD acceptance criteria.

### Notes
- Core runs fully offline (local hashing embeddings + heuristic merge). Hosted
  embeddings (OpenAI/Voyage) and LLM merge (Claude/GPT) are opt-in via the
  `[hosted]` extra.
