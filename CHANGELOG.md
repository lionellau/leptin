# Changelog

All notable changes to Leptin are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/); this project uses
[Semantic Versioning](https://semver.org/).

## [1.3.0] — 2026-06-20

The **credibility** release. An 8-persona adversarial review (senior engineers who
use Headroom) found the two flagship claims were *asserted, not measured*, and
oversold on the offline default. This release makes them true, measured, and
honestly scoped — the wedge that survives is **correctness-of-state over time**.

### Fixed (correctness blockers)
- **Contradiction-supersede no longer no-ops offline on real edits.** A graded
  detector (`leptin.llm.contradiction_signal`) confidently catches negation flips,
  antonyms, single-slot value swaps (`pnpm`→`bun`, `dark`→`light`), and numeric
  reversals (`14 days`→`30 days`) — and, crucially, **stops burying a true fact** on
  a loose numeric match (`8 cpu cores` vs `32 gb ram`). Uncertain conflicts are
  **flagged for review** (`leptin conflicts`), never silently coexisting or wrongly
  deleted. Verify it: `leptin bench --eval-contradiction` (bundled set: precision
  1.0, recall ~0.87, zero true facts buried).
- **The recall guardrail is no longer circular.** It verifies the *fact* still
  resolves (not just that an id survived), so a merge that keeps the id but drops the
  value is caught; it no longer carves "noise" out of its protected set; and it
  reports `low_confidence` / `verbatim_probe_fraction` so a lexical-embedder run is
  honest about its resolution.
- **Session-start injection now respects the budget.** Lessons are ranked and packed
  under a lesson sub-budget (`lesson_budget_frac`) with a `+N more` pointer, instead
  of bypassing the budget and growing unbounded; the push path now feeds the
  usefulness loop (it was invisible before).

### Added
- **Correctness benchmark** (`leptin bench`, now correctness-first): after a reversed
  decision a naive store serves the **outdated** fact 100% of the time vs Leptin **0%**
  (0% recall loss). The token number is split into **packing** (budget+floor) vs
  **governance** (dedup/supersede/decay) so it can't proxy for correctness.
- **Reframed flywheel:** recurrence (`recur_sessions`, a weak ranking tiebreaker) is
  separated from usefulness (explicit `record_feedback`, now also an MCP tool). A
  single `harmful` mark only down-weights and is reversible; it takes two to flag
  stale / drop guardrail protection. A strong, genuinely-used memory is never noise.
- **Reversible, discoverable supersede:** write-time supersedes get a reversible
  window and a review surface (`leptin superseded`); old rows are swept after the
  window, not leaked.
- **Bounded, demotable lessons:** auto-captured lessons are *candidates* that decay
  and graduate only on recurrence; auto-capture is gated on a real failure signal
  (not the substring "error"); `max_auto_lessons` caps the corpus. Hand-authored
  lessons stay permanent.
- **Embedder provenance + recovery:** every vector is tagged (`local-hash:256`); a
  hosted outage degrades *non-permanently* (cooldown + retry, not a permanent pin);
  `leptin reembed` re-vectorises; `doctor`/`health` flag embedder drift.
- **Scale + integrity:** LRU-bounded vector cache, subject-scoped (NULL-safe) dedup,
  optional `rank_candidate_limit` prefilter, 30s busy-timeout; deterministic tuner
  split (blake2b, not salted `hash()`); a floor-free normalized health score; and the
  noise/penalty/lesson/decay constants promoted to `Config` (locked against the tuner).
- `record_feedback` MCP tool; `LEPTIN_MCP_TOOLS` now also accepts an explicit list.

### Changed
- Docs/positioning scoped honestly: the supersede guarantee names its offline limits;
  the 66% is attributed (packing vs governance); "runs on top of YOUR store" softened
  to "self-contained store *with* a correctness loop, runs alongside your compressor"
  (the external-store governor is roadmap, not shipped). `config.backend != sqlite`
  now warns and falls back instead of silently doing nothing.

### Storage
- Schema v5: `recur_sessions`, `last_inject_at`, `embedder`, `conflicts_with`
  (migrates in place; additive, reversible).

## [1.2.0] — 2026-06-20

The **feedback-loop** release. Sharpens the positioning from "memory governor" to a
**control loop for agent memory** — the discipline lives on the harness's hooks
(push), not behind tools the model has to call (pull) — and adds the loops that make
the store get *more correct and more useful with use*, the axis a plain store or a
context-compressor doesn't cover. See [docs/loops.md](docs/loops.md).

### Added
- **Auto mistake-capture (post-tool loop).** The `PostToolUse` hook distills a failed
  tool call into a never-decaying lesson automatically — re-injected next session, so
  the agent doesn't repeat it. (`leptin hook post-tool-use`.)
- **Recall-usefulness flywheel.** Memories now track `inject_count`, `useful_count`,
  and `harmful_count`. Memories that recur across sessions or are marked useful get
  reinforced; memories injected repeatedly but never useful are treated as **noise**
  and become prune candidates — under the same recall guardrail. `leptin feedback
  <id>... [--harmful]` closes the loop by hand.
- **Memory-health score.** `leptin health` grades the store 0–100 (A–D) on stale rate,
  noise rate, and harmful hits, with drift flags; also surfaced in `diet_report`.
- **`docs/loops.md`** — design note on why Leptin is a loop on the harness, not an
  MCP tool the model must remember to call.

### Changed
- Repositioned README/docs/package metadata around the **control loop** (harness +
  hooks) rather than the MCP surface; added the loop diagram (`assets/loop.svg`).
- `derive_probes` now treats importance as *useful*, not merely *injected*, so the
  guardrail no longer protects noise from its own (safe) pruning.

### Storage
- Schema v4: adds `inject_count`, `useful_count`, `harmful_count`, and
  `last_inject_session` to `memories` (migrates in place; additive, reversible).

## [1.1.0] — 2026-06-20

Repositioned from a token-saving store into a **memory governor**: keep long-term
memory *correct and current*, and forget only when recall is provably preserved.

### Added
- **Memory typing** (`fact` / `procedural` / `task` / `lesson`) with per-type decay.
- **Never-decaying lessons-learned** (`leptin lesson "..."`) — stored once, and
  **auto-injected at every session start** so the agent stops repeating mistakes.
- **Provenance anchoring** (`--source-ref`, e.g. `linear:ABC-123`, `spec:foo.md#sec`)
  + `leptin stale <ref>` to flag memories whose source changed (down-weighted in recall).
- **Lifecycle hooks** for Claude Code + Codex: `leptin hook <event>` emits memory +
  lessons as `additionalContext` at SessionStart/UserPromptSubmit, and runs guardrailed
  compaction at Stop/PreCompact. `leptin connect claude-code|codex` prints the wiring.
- `session_context` API + `remember_lesson` / `lessons` / `flag_stale`.

### Changed
- **Lean MCP surface:** only `recall` + `remember` are exposed to the model by default
  (discipline runs via hooks/CLI, not as model-callable tools). `LEPTIN_MCP_TOOLS=all`
  restores the full set. Removes per-request tool-schema token overhead.
- README/positioning reframed outcome-first (correct & current memory; lessons that
  stick; verified forgetting), and honest about fitting *alongside* storage/compression
  layers rather than replacing them.

### Migrations
- Schema v2 → v3 (adds `mtype`, `source_ref`, `stale`); older stores upgrade in place.

Tests: 112 → 122.

## [1.0.0] — 2026-06-19

**First stable release.** Leptin is feature-complete for its PRD scope and
production-ready: a drop-in MCP memory server with token-budgeted recall, an
auditable savings ledger, an identity-based recall guardrail, glass-box
reversibility, a reproducible offline benchmark, a local dashboard, a TypeScript
SDK, and closed-loop **self-tuning**. Zero required dependencies; runs fully
offline; 99 tests; CI on Python 3.10–3.13.

### Real-world / production hardening
- **`leptin doctor`** — health check (store, schema version, memory counts, size,
  embedding/LLM model + hosted SDK/API-key readiness, self-tuning + guardrail
  status); exits non-zero if unhealthy.
- **Schema migrations** — versioned on-disk schema (`PRAGMA user_version`);
  databases from older versions upgrade in place on open, data preserved.
- **Concurrency** — `busy_timeout` so the server, dashboard, and CLI share one
  DB file without "database is locked" errors (multi-writer test).
- **Scale** — parsed-embedding cache keeps recall in the low-ms over thousands
  of memories (latency test).
- **Hardened hosted mode** — embedding/LLM calls retry transient errors with
  backoff before degrading; per-text embedding cache avoids re-billing; one-time
  downgrade warning. Never silently degrades.
- **Structured logging** — `LEPTIN_LOG` level control, stderr only.
- **Real-dataset benchmark** — `leptin bench --dataset <locomo.json>
  --embedding-model …` runs the harness on real LoCoMo data (synthetic stays the
  offline default).
- Tests: 99 → 112.

This release promotes the complete, twice-audited feature set below (0.1.0 +
0.2.0) to stable — API and on-disk schema are now considered committed under
semantic versioning (schema migrations guarantee forward-compatible upgrades).

The forward roadmap (backend adapters for Mem0/pgvector, hosted prompt/intent
tuning, async tuning daemon, `sqlite-vec` fast path) is post-1.0 enhancement
work; none of it is required for the product to be complete and useful today.

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
