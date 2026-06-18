# Changelog

All notable changes to Leptin are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

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
