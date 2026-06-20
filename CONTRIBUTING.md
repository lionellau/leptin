# Contributing to Leptin

Thanks for helping put agent memory on a diet. Leptin is small, dependency-free,
and test-driven on purpose — contributions that keep it that way are very welcome.

## Dev setup

```bash
git clone https://github.com/lionellau/leptin
cd leptin
uv venv && uv pip install -e ".[dev]"     # or: python -m venv .venv && pip install -e ".[dev]"
pytest                                      # full suite (no API key needed)
leptin bench                                # reproduce the headline number
```

Everything runs **fully offline** — the core has zero third-party dependencies.
Hosted embeddings/LLM live behind the `[hosted]` extra and are exercised in
tests with mock SDKs (`tests/test_hosted.py`), so you never need an API key to
develop or to land a change.

## Ground rules

- **Keep the core dependency-free.** New third-party deps in the core are a hard
  no. If a feature needs a library, gate it behind an optional extra and degrade
  gracefully when it's absent (see `embeddings.py` / `llm.py` for the pattern).
- **Tests with every change.** New behaviour needs a test; bug fixes need a
  regression test. Map tests to the relevant PRD acceptance criterion where one
  exists.
- **The guardrail is sacred.** Anything touching `guardrail.py` / compaction must
  preserve the invariant: a committed compaction never drops recall of a probed
  or high-strength fact, and nothing is hard-deleted inside the retention window.
- **The ledger must stay honest.** Headline `tokens_saved` is recall *injection*
  savings only; one-time/reversible reductions go to `footprint_tokens_reduced`.
  Don't fold them together.
- Match the surrounding style; prefer clarity over cleverness; keep comments to
  the "why".

**Design rule (non-negotiable):** core dependencies stay `[]` forever — the
instant, offline `pip install` is the whole reason this ICP (an individual or
small team running their own coding agent) picks Leptin. Any semantic-model path
ships only as an opt-in extra (e.g. `leptin-hlp[hosted]`); the default must never
require an account, an API key, or a download.

## Good first contributions

- **Host installers** — make `leptin setup` work for more agents (Cursor, Gemini
  CLI, Windsurf) and keep the self-install path agent-runnable. This is the
  highest-leverage area: it's how an agent installs Leptin on itself.
- A better **offline tier** that stays zero-dep (smarter lexical matching,
  dev-term normalization) — the free path most people run.
- More benchmark corpora / a LongMemEval harness; a real-LoCoMo result.
- Memory/audit dashboard polish.
- *Also welcome (roadmap, not the current focus):* a governor mode over an
  external store (Mem0 / pgvector) via `config.backend`, and a `sqlite-vec` fast
  path — these serve the larger-scale / embed-as-a-component case, which is not
  the personal-infrastructure wedge.

## Submitting

1. Branch from `main`.
2. `pytest` and `leptin bench` must pass (CI runs both on 3.10–3.13).
3. Open a PR describing the change and the user-visible effect. Link an issue if
   there is one.

By contributing you agree your work is licensed under the project's MIT license.
