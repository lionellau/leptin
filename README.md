<div align="center">

# 🧬 Leptin

### The satiety hormone for agent memory.

**A drop-in MCP memory server that puts your agent's long-term memory on a token budget, shows you the receipts, and guarantees it never silently forgot anything that mattered.**

[![tests](https://img.shields.io/badge/tests-99%20passing-3fb950)](#testing)
[![self-tuning](https://img.shields.io/badge/self--tuning-closed--loop-3fb950)](#-self-tuning-leptin-learns-its-own-diet)
[![benchmark](https://img.shields.io/badge/LoCoMo--mini-66%25%20fewer%20tokens%20%40%200%25%20recall%20loss-3fb950)](#the-headline-reproduce-it-yourself)
[![python](https://img.shields.io/badge/python-3.10%2B-58a6ff)](#install)
[![deps](https://img.shields.io/badge/core%20deps-zero-58a6ff)](#design)
[![license](https://img.shields.io/badge/license-MIT-8b98a9)](LICENSE)

</div>

---

Persistent-memory MCP servers fixed *"the agent forgets between sessions."* But they created a new, invisible problem: **the memory store silently inflates every prompt and bills you for it.** As the store grows, each `recall` dumps more matched memories into context — eating the very window it was meant to protect — and you have **zero visibility** into the cost. The few tools that *do* forget make you migrate your whole stack and give you **no proof** that forgetting didn't drop something important.

**Leptin is the missing diet + scale + safety net.** Like the hormone it's named after, it tells your memory store when it's had enough — so it stops hoarding.

```
  Other memory layers either grow forever and bill you in the dark,
  or forget things and hope you don't notice.

  Leptin puts your memory on a budget, shows you the receipts,
  and proves it didn't forget anything that mattered.
```

---

## The headline (reproduce it yourself)

```bash
leptin bench
```

```
  Leptin benchmark — naive top-k store vs. Leptin (offline, deterministic)
  ----------------------------------------------------------------
  corpus            : 49 inserts, 24 probes
  active memories   : naive   47   leptin   39   (dedup kept 8 out)
  recall budget     : 1500 tokens   |   naive dumps top-10
  ----------------------------------------------------------------
  memory tokens     : naive   3396   leptin   1147
  TOKEN REDUCTION   : 66.2%   (target ≥ 60%)
  recall            : naive 1.000   leptin 1.000
  RECALL LOSS       : 0.0%   (target ≤ 2%)
  est. $ saved      : $0.006966  (priced at claude-sonnet-4-6)
  recall latency    : 2.18 ms/query (leptin)
  ----------------------------------------------------------------
  HEADLINE          : PASS ✅  ≥60% fewer memory tokens at ≤2% recall loss
  models            : embedding=local-hash, llm=heuristic
```

> **≥60% fewer memory tokens at ≤2% recall loss** — runs fully offline, no API key, deterministic. The corpus, prompts, and models are pinned in code so the number is the same on your machine as on ours.

<details>
<summary><b>About this benchmark — what it does and doesn't show</b></summary>

Being honest is the point of a tool that sells "the receipts":

- **The baseline is a naive top-k dump** — exactly what stock persistent-memory MCP servers do today (recall returns the top-k matches and injects all of them). That's the real status quo Leptin competes against, not a strawman.
- **Savings come from two real mechanisms:** mostly *budgeted, relevance-packed recall* (inject what's on-topic under a ceiling, not a fixed 10), plus *write-time dedup/merge* (a smaller store → leaner matches). The output shows the dedup contribution separately (`dedup kept N out`).
- **The corpus is synthetic and illustrative** — a bundled, deterministic LoCoMo-style set with the redundancy and contradictions real stores accumulate. It is designed to be reproducible offline, not to stand in for a peer-reviewed result.
- **To measure *your* numbers**, install `leptin-mcp[hosted]`, configure real embeddings/LLM, and run `leptin bench` against your own data — the same harness measures your hosted setup.

</details>

---

## Quickstart

### 1. Install

```bash
pip install leptin-mcp                 # once published to PyPI
uvx leptin-mcp serve                   # zero-install run (uv)

# until then — straight from the repo:
pip install "git+https://github.com/lionellau/leptin"
# or from a clone:
git clone https://github.com/lionellau/leptin && cd leptin && pip install -e .

# optional: hosted embeddings + LLM merge (OpenAI / Voyage / Claude)
pip install "leptin-mcp[hosted]"
```

### 2. Connect it to Claude Code / Codex

```bash
leptin init
```

That prints a ready-to-paste MCP config block:

```jsonc
{
  "mcpServers": {
    "leptin": {
      "command": "leptin",
      "args": ["serve", "--db", "~/.leptin/memory.db"]
    }
  }
}
```

Restart the client. The agent now has 8 memory tools. Ask it to *"remember that I prefer dark mode"*, then later *"what are my preferences?"* — and run `leptin report` to see the tokens and dollars saved.

> Savings show up once your store has some overlap (so dedup/merge fires) or recall hits the token budget. On a brand-new store with one fact, `report` will honestly say it hasn't saved anything *yet* — keep using it and the receipts add up.

### 3. See the receipts

```bash
leptin dashboard      # opens a local savings dashboard at http://127.0.0.1:8765
leptin report         # or just print the ledger as JSON
```

---

## What you get — the four-part wedge

The "compress/forget memory" idea is partially solved. The unowned gap is the **intersection** of these four — no other tool ships all of them:

| | Pain | Leptin |
|---|---|---|
| 🪶 | *"Recall keeps blowing my context window."* | **Token-budgeted, packed recall** — a hard ceiling per injection; memories are packed for max relevance under the budget, not dumped top-k. |
| 🧾 | *"I can't see what my memory layer costs me."* | **A savings ledger on YOUR data** — every op records baseline vs. actual tokens and converts to $. Not a vendor's benchmark slide. |
| 🛟 | *"If I turn on forgetting I might lose something."* | **A recall guardrail** — before any prune commits, it re-probes the store and **auto-rolls-back** anything that would hurt recall. The trust mechanism nobody ships. |
| 🔌 | *"I don't want to migrate my whole stack."* | **A sidecar, not a store** — works standalone on SQLite (zero infra) or, soon, on top of the Mem0 / pgvector backend you already run. |

---

## The 8 MCP tools

| Tool | What it does |
|---|---|
| `remember` | Store a fact. Write-time **dedup/merge**; contradictions **supersede** the older fact (kept, not deleted). |
| `recall` | Retrieve under a **token budget** — packed for relevance, with `tokens_saved` vs. a naive top-k dump. |
| `compact` | **Guardrailed** decay-prune + merge + supersede. Auto-rolls-back any prune that hurts recall. `dry_run` to preview. |
| `forget` | Soft-delete by id or query → **quarantine** (reversible), never a hard delete. |
| `restore` | Bring a forgotten/quarantined memory back. Glass-box reversibility. |
| `inspect` | Full provenance, current strength, and event history for any memory. |
| `diet_report` | The "show me the receipts" tool: tokens & $ saved, op breakdown, guardrail status, top savers. |
| `self_tune` | **Self-evolve the memory policy** on your data — commit only on a proven net win, else revert. Offline, zero LLM calls. |

---

## How it works

Five mechanisms, all behind the MCP interface:

1. **Write-time dedup / merge** — on `remember`, near-duplicates within a subject are merged into one canonical memory; contradictions supersede the older fact. The store stops accumulating restatements.
2. **Time-decay forgetting** — each memory has a `strength` that decays exponentially (Ebbinghaus-style, configurable half-life) and is boosted on access. Weak memories become prune-eligible.
3. **Budgeted, packed recall** — candidates ranked by `similarity × strength`, then greedy-packed under the token budget with a relevance gate, so off-topic padding never makes the cut.
4. **Savings ledger** — every op logs `baseline_tokens` (what a naive store would have injected) vs. `actual_tokens`, converted to $ via a per-model price table.
5. **Recall guardrail** — a probe set (`question → expected_fact`, auto-derived from high-strength memories + your own probes) is re-run after each compaction *inside a transaction*; if recall would drop past a threshold, the whole prune is **rolled back**. Coverage is checked by *memory identity* (did the protected memory — or the one that supersedes/merged it — actually survive and get recalled?), so an unrelated entry that merely shares a word can never mask a real loss.

> **What the guardrail guarantees, precisely:** compaction never degrades recall of your **important** facts — anything frequently used (high strength) or anything you've added a probe for. Decay still prunes genuinely *cold* facts (low strength, never queried, no probe) — but those are **quarantined and fully restorable** within the retention window, never hard-deleted. If you care about a specific cold fact, add a probe and it becomes guardrail-protected.

### The guardrail, concretely

```python
mem.remember("The encryption key rotates every 90 days.", subject="security")
mem.add_probe("how often does the key rotate?", "90 days")

# ... 60 days pass, the memory's strength decays below the floor ...

mem.compact()
# -> guardrail: { recall_before: 1.0, recall_after: 0.0, passed: False, rolled_back: True }
# The prune that would have dropped a probed fact was automatically undone.
# The memory is still active and recallable. Nothing was silently forgotten.
```

---

## 🧬 Self-tuning — Leptin learns its own diet

Leptin doesn't just *measure* itself — it **evolves**. The self-tuning loop replays your own data under candidate policies and commits a change only when held-out evals prove it's a net win (more savings, no recall loss), else it leaves the config alone. Same trust DNA as the guardrail, applied to the policy itself.

```bash
leptin tune --dry-run     # preview the proposed change
leptin tune               # apply it (only if it's a proven net win)
leptin tune --history     # the evolution ledger
leptin tune --rollback    # undo the last change (exactly)
```

```jsonc
// leptin tune  →
{ "accepted": true,
  "changes": [{ "knob": "recall_rel_floor", "old": 0.40, "new": 0.42, "direction": "up" }],
  "objective_before": 0.682, "objective_after": 0.718,
  "recall_before": 1.0, "recall_after": 1.0,
  "llm_calls": 0, "tune_tokens": 0 }     // ← offline: costs nothing
```

**How it stays safe and cheap:**

- **Held-out gate + dual-metric accept.** Probes are split (4/5 visible to the optimizer, 1/5 held out); a change commits only if it improves the objective AND holds recall on the held-out set. No overfitting the eval, no recall regressions.
- **Locked knobs.** The optimizer (a UCB coordinate-ascent hill-climb) can only touch a clamped set of recall/decay knobs. It can **never** touch `guardrail_max_drop` or other safety rails.
- **Reversible.** Every accepted change is a row in an evolution ledger; `leptin tune --rollback` restores the exact prior config. A meta-guardrail freezes the *automatic* loop after repeated failures.
- **Token/context efficient by construction.** Read-only candidate evals on a bounded query sample, **zero LLM calls offline**, cadence-triggered (not per-op), tiny scorecard output. Auto-tuning is opt-in (`self_tune_enabled`); manual `leptin tune` always works.

Turn on the autopilot:

```bash
LEPTIN_SELF_TUNE_ENABLED=true leptin serve --db ~/.leptin/memory.db
```

`diet_report` (and the dashboard) grow a `tuning` block so the self-evolution is a glass box too: cycles, accepted/rejected, current version, and `llm_calls` (0 offline).

---

## Design

- **Zero core dependencies.** The engine, MCP server, ledger, guardrail, dashboard, and benchmark run on the Python standard library alone. No native vector extension to fight with, no model download. `pip install` is instant; `uvx leptin-mcp` just works.
- **Offline by default, hosted by upgrade.** The default embedder is a deterministic hashing vectorizer and merges are heuristic — so everything (including the benchmark) runs with no API key and is reproducible. Install `leptin-mcp[hosted]` and set a model to use real OpenAI/Voyage embeddings + Claude/GPT-powered merging.
- **Graceful degradation.** If the embedding/LLM API is unreachable, `remember`/`recall` never throw — they fall back to local embeddings / keyword recall so your agent never breaks.
- **Glass box, fully reversible.** Every merge/decay/forget is logged with a reason; nothing is hard-deleted within the retention window.

> ⚠️ **Offline-mode caveat:** the default hashing embedder merges *near-lexical* duplicates and restatements well, but not deep paraphrases (*"dark mode"* vs *"night theme"*). For semantic dedup, configure hosted embeddings. The conservative defaults err toward **keeping** data — consistent with "never silently forget."

---

## Configuration

Every tunable has a sane default. Override via env vars (`LEPTIN_*`), the `config` table, or the `Config` object:

| Key | Default | Meaning |
|---|---|---|
| `token_budget_default` | `1500` | Hard token ceiling per recall |
| `dedup_threshold` | `0.86` | Cosine τ for near-duplicate merge |
| `decay_half_life_days` | `14` | Strength halving time |
| `strength_floor` | `0.15` | Below this → prune-eligible |
| `guardrail_max_drop` | `0.02` | Max tolerated recall drop before rollback |
| `recall_rel_floor` | `0.55` | Inject only memories this relevant vs. the best match |
| `embedding_model` | `local-hash` | or `text-embedding-3-small`, `voyage-3`, … |
| `llm_model` | `heuristic` | or `claude-haiku-4-5`, `gpt-4o-mini`, … |
| `price_model` | `claude-sonnet-4-6` | Which prices to value savings against |
| `self_tune_enabled` | `false` | Run the self-tuning loop automatically after compaction |
| `tune_objective` | `balanced` | `balanced` / `savings` / `recall` weighting |

---

## Python API

```python
from leptin.api import Leptin

mem = Leptin("~/.leptin/memory.db")
mem.remember("The backend is FastAPI on Postgres.", subject="stack")
print(mem.recall("what's the backend?", token_budget=500))
print(mem.diet_report("all"))   # tokens & $ saved
mem.close()
```

---

## CLI

All commands are also available as `python -m leptin <command>` if the console
script isn't on your PATH.

```bash
leptin serve   --db PATH        # run the MCP server on stdio
leptin bench   [--budget N]     # reproducible token-savings benchmark
leptin init    [--db PATH]      # create a store + print the MCP config block
leptin report  [--window all]   # print the savings ledger
leptin dashboard                # local savings dashboard
leptin remember "..." [--subject S]
leptin recall  "..." [--budget N]
leptin compact [--dry-run]
leptin inspect [--query "..."]
```

---

## Testing

```bash
uv venv && uv pip install -e ".[dev]" && pytest
```

99 tests cover the PRD acceptance criteria: budget guarantees (incl. `token_budget=0`), the savings-ledger math, dedup/merge/supersede, decay, the guardrail rollback/commit invariants, **self-tuning** (offline zero-cost, lock enforcement, improvement-on-degraded-store, reversibility, determinism), glass-box reversibility, the MCP protocol surface (including a real `leptin serve` subprocess driven over stdio), the dashboard HTTP layer, the hosted OpenAI/Voyage/Anthropic integration + degradation paths (mock-verified), env config coercion/clamping, and the reproducible benchmark. CI runs the suite, the benchmark, a clean wheel install, and the TS build on Python 3.10–3.13.

---

## Roadmap

- **v0.1:** MCP server + tools · SQLite backend · dedup/merge · decay · budgeted packed recall · savings ledger · recall guardrail + reversibility · `leptin bench` · local dashboard · `@leptin/client` TS SDK.
- **v0.2 (now):** 🧬 **self-tuning** — closed-loop policy evolution with held-out evals, evolution ledger + rollback, meta-guardrail; merge/supersede-on-compact; offline zero-cost.
- **v0.3 (next):** async tuning daemon · hosted prompt/intent optimization (opt-in) · recency-of-miss probe sampling · Mem0 adapter · `sqlite-vec` fast path.
- **Later:** pgvector / knowledge-graph backends · shared/team memory.

---

## License

MIT — see [LICENSE](LICENSE).
