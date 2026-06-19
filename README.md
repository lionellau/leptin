<div align="center">

# 🧬 Leptin

### A control loop for agent memory.

**Leptin is a local-first *control loop* that keeps your coding agent's long-term memory correct over time. It rides your agent's existing harness — the session-start, post-tool, and pre-compact hooks already firing every loop — to resolve contradictions, retire stale facts, make hard-won lessons stick, and learn which memories actually help. So the agent stops acting on outdated information and stops repeating mistakes — and before it ever forgets anything, it *proves* you can still recall what matters.**

[![CI](https://github.com/lionellau/leptin/actions/workflows/ci.yml/badge.svg)](https://github.com/lionellau/leptin/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/leptin-mcp?color=3fb950)](https://pypi.org/project/leptin-mcp/)
[![python](https://img.shields.io/badge/python-3.10%2B-58a6ff)](#install)
[![core deps](https://img.shields.io/badge/core%20deps-zero-58a6ff)](#design)
[![tests](https://img.shields.io/badge/tests-131%20passing-3fb950)](#testing)
[![license](https://img.shields.io/badge/license-MIT-8b98a9)](LICENSE)

[Quickstart](#quickstart) · [Why I built this](#why-i-built-this) · [How it works](#how-it-works) · [Who it's for](#who-leptin-is-for) · [Where it fits](#where-leptin-fits-vs--alongside-other-tools) · [Security](#security)

<img src="assets/demo.svg" alt="Leptin: lessons injected at session start, recall under a token budget, verified forgetting" width="760"/>

</div>

---

A persistent memory layer is supposed to make your agent smarter over time. Left ungoverned it does the opposite: it accumulates **duplicates, stale facts, and outright contradictions**, so the agent ends up *confidently wrong* — "use pnpm" survives six weeks after you switched to bun — and it repeats mistakes you already taught it not to make. The tools that prune to fix this do it **blindly**: you can't see what was dropped, can't get it back, and have no guarantee a useful fact wasn't deleted.

**Leptin is the *loop* that keeps memory correct — not another store.** A store answers "what did I save?"; Leptin closes the loop around it: every session, it dedups, supersedes contradictions so the *current* truth wins, decays what's cold, keeps your **lessons-learned permanent** and re-injects them automatically, watches which memories actually get used, and never prunes without first **proving recall didn't regress** — rolling back if it did. It plugs into the harness your agent already runs (hooks), and sits *alongside* whatever store or context-compressor you already use.

> Two outcomes you can feel:
> 1. your agent stops acting on **stale / contradictory** memory, and
> 2. your agent stops **repeating mistakes** it already learned from.

---

## Why I built this

> *(The origin story — the "why" behind the code.)*

I run coding agents all day on long-lived projects, with a persistent-memory MCP server so the agent stops re-learning my stack every session. It worked — until the store grew. Then `recall` started handing back a noisy pile of half-relevant, sometimes *contradictory* memories: a decision we'd reversed, a convention we'd dropped. The agent acted on the stale one. And it kept making the same mistakes across sessions because nothing made "we learned X the hard way" stick.

The ecosystem is good and moving fast — plenty of tools store, compress, and dedup memory well. What I couldn't find was the piece that keeps memory **correct over time**: resolve contradictions so the *current* truth wins, retire cold facts safely, keep lessons permanent, and prove a prune never cost me a fact I'd query later. So I built it, for the way I work — a solo dev on a local store I'd rather not migrate off of — and published it in case your setup looks like mine.

*— [@lionellau](https://github.com/lionellau). PRs, issues, and "this stopped my agent repeating X" stories welcome.*

---

## How it works

Leptin is a loop, not a tool the model calls. Each turn of your agent's harness fires it: **session-start** injects lessons + current truth, **post-tool** captures mistakes the moment they happen, **pre-compact / stop** runs the guarded prune. The loop spans **three layers** — and the model-facing one is deliberately tiny.

<div align="center">
<img src="assets/loop.svg" alt="The Leptin control loop: inject → act → capture → reconcile → verify → forget" width="760"/>
</div>

> Why the loop, not just an MCP tool? An MCP server is *pull* — it works only when the model remembers to call it. Memory hygiene can't depend on that. Leptin runs on the harness's *push* points (hooks) so the discipline happens every loop whether or not the model asks. The MCP surface is just the two tools (`recall`/`remember`) the model genuinely needs in-band. See [docs/loops.md](docs/loops.md).

<div align="center">
<img src="assets/architecture.svg" alt="Leptin architecture" width="900"/>
</div>

1. **Discipline layer (hooks + CLI/daemon) — not model-callable.** Decay, dedup, supersede, the recall guardrail, and self-tuning run automatically through lifecycle hooks and a background CLI. They are *not* exposed as tools the model has to remember to call (tool schemas are a per-request token tax — eight of them is a standing cost for work the model should never invoke).
2. **Query layer (MCP) — lean.** Only **`recall`** and **`remember`** are exposed to the model by default. (Need the rest as tools? `LEPTIN_MCP_TOOLS=all`.)
3. **Human layer (CLI + dashboard).** Receipts (tokens & $ saved), the evolution/guardrail audit, `leptin doctor`, tuning.

The defining mechanisms:

- **Memory typing.** Every memory has a type with its own decay: `fact` (normal), `procedural` (slow), `task` (fades with the ticket), and **`lesson` — never decays.**
- **Lessons-learned, auto-injected.** Store an anti-pattern once (`leptin lesson "..."`); it's re-injected at **every session start** via a hook, so the agent stops repeating it. No tool call required.
- **Contradiction resolution (supersede).** A newer fact that conflicts with an older one wins; the old one is kept, marked superseded, and stays auditable. Recall returns the *current* truth, not both.
- **Verified, transactional forgetting (the guardrail).** Before any prune commits, Leptin re-runs a probe set against the post-prune store *inside a transaction*; if recall would regress, the whole prune **rolls back**. Nothing is silently lost, and everything is reversible.
- **Provenance anchoring.** Anchor a memory to its source (`linear:ABC-123`, `spec:auth.md#flow`, `commit:sha`). When the source changes, `leptin stale <ref>` flags it — stale memories are down-weighted, not blindly trusted ("a fact is confidently wrong the moment its source changes").
- **Auto mistake-capture (the post-tool loop).** When a tool call fails (a bad command, a broken build), the post-tool hook distills it into a never-decaying lesson automatically — no one has to remember to write it down. Next session it's re-injected, so the agent doesn't walk into the same wall twice.
- **Recall-usefulness flywheel.** Leptin tracks which memories get injected, which recur across sessions (a useful signal), and which you flag as harmful (`leptin feedback <id> --harmful`). Memories that prove useful are reinforced; memories injected again and again but never useful are treated as **noise** and become prune candidates — so the store gets *more* relevant the more you use it, not just bigger.
- **Memory-health score.** `leptin health` grades the store 0–100 (A–D) on stale rate, noise rate, and harmful hits, and flags drift before recall quality rots — an at-a-glance read on whether the loop is keeping memory clean.
- **Budgeted, packed recall + savings ledger.** Recall is capped and packed for relevance; every op logs tokens (and $) saved vs. a naive top-k dump.

---

## Quickstart

### 1. Install
```bash
pip install leptin-mcp                 # once published to PyPI
pip install "git+https://github.com/lionellau/leptin"   # from source today
# optional hosted embeddings + LLM merge:
pip install "leptin-mcp[hosted]"
```

### 2. Wire it into Claude Code / Codex (hooks + lean MCP)
```bash
leptin connect claude-code     # prints the config: lifecycle hooks + the 2-tool MCP server
```
This installs the **discipline as hooks** (memory auto-injected at session start; compaction on stop) and exposes only `recall` + `remember` to the model.

### 3. Teach it a lesson, then watch the loop keep it correct
```bash
leptin lesson "Never run DB migrations on a Friday deploy."
# next session, that lesson is injected automatically — the agent won't repeat it
leptin remember "Auth uses JWT in cookies." --subject auth --source-ref spec:auth.md#tokens
leptin stale spec:auth.md#tokens     # when the spec changes, the memory is flagged
leptin feedback <id> --harmful       # close the loop: tell Leptin a recall misled the agent
leptin health                        # 0–100 score: is the loop keeping memory clean?
leptin dashboard                     # the receipts (tokens & $ saved, the audit trail)
```

<div align="center"><img src="assets/dashboard.png" alt="Leptin dashboard" width="760"/></div>

---

## Who Leptin is for

The value shows up when an agent's memory **accumulates over time** and gets queried a lot:

- **Spec-driven / long-running coding** — weeks on one codebase → hundreds of facts, changing decisions. Leptin keeps the *current* decision authoritative and stops the agent acting on reversed ones.
- **Long-horizon research** — findings/sources pile up across sessions with updates and contradictions; Leptin keeps the knowledge base current and auditable.
- **Autonomous / looping / scheduled agents** — they write and recall constantly; verified pruning + never-decaying lessons keep memory from rotting or running away.
- **Small teams (2–10) working from specs + Linear tickets** — provenance anchoring ties memory to the ticket/spec so stale specs stop misleading the agent.

**You probably don't need Leptin if:** you do one-off Q&A / daily ops with no growing memory; your work fits a single session; or your store stays tiny.

---

## Where Leptin fits (vs / alongside other tools)

This isn't a teardown — the memory space is strong and these are good tools. Leptin sits on a **different axis**: most tools answer *"what do I store and how small can I make it?"* Leptin answers *"is what I'm storing still correct, and is it actually helping?"* — it's a control loop, not a store or a compressor, and it runs **alongside** them.

- **vs storage layers (Mem0, vector stores):** they store and retrieve by similarity — excellent at that. They don't adjudicate which conflicting fact is *currently true*, verify that a prune didn't cost you a fact, or learn which memories actually earned their place. Leptin closes that loop, and can sit **on top of** your store.
- **vs context-compression layers (e.g. Headroom):** they shrink what the agent reads each turn (great at that) and keep everything. Leptin works on the other end — the long-term store's *correctness over time*: superseding stale truth, keeping lessons permanent, pruning proven-noise under a recall guardrail. Compress the stream **and** govern the store; they don't overlap.

If you need a managed, hosted memory platform, use one. Leptin is the small, local, auditable loop that keeps long-term memory **correct and useful** — and plugs into whatever else you run.

---

## Design

- **Zero core dependencies** — engine, MCP server, hooks, guardrail, dashboard, benchmark, self-tuner all run on the Python stdlib. `pip install` is instant.
- **Offline by default, hosted by upgrade** — deterministic hashing embeddings + heuristic merge need no API key; `leptin-mcp[hosted]` adds OpenAI/Voyage embeddings + Claude/GPT merging (with retry + caching).
- **Glass box, reversible** — every merge/decay/forget/supersede/tune is logged with a reason; nothing is hard-deleted within the retention window; schema is versioned and migrates in place.

> Offline-mode caveat: the default hashing embedder catches *near-lexical* duplicates, not deep paraphrases. Configure hosted embeddings for semantic dedup. Defaults err toward **keeping** data.

---

## Self-tuning (optional)

Leptin can tune its own policy on your data: it replays under candidate configs and commits a change only if held-out evals prove a net win, else reverts — offline, **zero LLM calls**, fully reversible (`leptin tune`). The guardrail's safety knobs are locked and never tuned.

---

## Security

Leptin is **local-first**: the MCP server speaks stdio (no network listener); the dashboard binds to 127.0.0.1 with a Host-header guard; memory content is data, never executed; hosted calls read keys from env, never stored; a user's memory DB is git-ignored (`*.db`/`*.sqlite`). Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

---

## Reproducible benchmark

```bash
leptin bench          # offline, deterministic
```
On the bundled synthetic corpus, budgeted/packed recall returns **~66% fewer tokens than a naive top-k dump at 0% recall loss** — a narrow, reproducible claim about *recall payload size* (run it on real [LoCoMo](https://snap-research.github.io/locomo/) with `--dataset locomo.json --embedding-model text-embedding-3-small`).

---

## Testing

```bash
uv venv && uv pip install -e ".[dev]" && pytest
```
131 tests cover memory typing + never-decaying lessons, contradiction-supersede, the guardrail rollback/commit invariants, provenance/staleness, the recall-usefulness flywheel (inject/useful/harmful counters, noise pruning under the guardrail), auto mistake-capture via the post-tool hook, the memory-health score, the hook entrypoint (SessionStart injection), the lean vs `all` MCP surface (incl. a real `leptin serve` subprocess), schema migrations, the savings ledger, self-tuning, the dashboard HTTP layer, hosted integration + retry/degradation, and the reproducible benchmark. CI runs the suite, the benchmark, a clean wheel install, and the TS build on Python 3.10–3.13.

---

## CLI

```bash
leptin connect claude-code|codex   # wire hooks + lean MCP
leptin serve   --db PATH           # MCP server (stdio)
leptin hook    <event>             # lifecycle-hook entrypoint (used by connect)
leptin lesson  "..."               # store a never-decaying lesson
leptin remember "..." [--type fact|procedural|task|lesson] [--source-ref REF]
leptin stale   <source-ref>        # flag memories whose source changed
leptin feedback <id>... [--harmful]  # close the loop: reinforce or down-weight a memory
leptin health                      # 0–100 memory-health score (stale / noise / harmful, A–D)
leptin recall  "..." [--budget N]
leptin compact [--dry-run]    leptin tune [--dry-run|--rollback|--history]
leptin doctor   ·   leptin dashboard   ·   leptin report   ·   leptin bench
```

---

## Roadmap

**Shipped:** memory typing + never-decaying lessons · contradiction-supersede · verified transactional forgetting (guardrail + rollback) · provenance anchoring + staleness · the full control loop on lifecycle hooks (Claude Code + Codex): SessionStart injection, **post-tool auto mistake-capture**, pre-compact guarded prune · **recall-usefulness flywheel** (inject/useful/harmful + noise pruning) · **memory-health score** · lean MCP surface · budgeted recall + savings ledger · self-tuning · local dashboard · `leptin doctor` · schema migrations · 131 tests + CI.

**Next:** a governor/adapter mode that runs the loop over an existing store (Mem0 / pgvector) so you keep your backend · more source adapters (Linear / GitHub / Jira / spec watchers) · more host installers (Cursor, Gemini CLI) · `sqlite-vec` fast path.

---

## Contributing

PRs welcome — especially **source adapters** and **host installers**. See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Keep the core dependency-free, add a test, and don't weaken the guardrail (its safety knobs stay locked).

## License

MIT — see [LICENSE](LICENSE).

<div align="center"><br/><i>If Leptin kept your agent's memory honest, a ⭐ helps others find it.</i></div>
