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

## Good first contributions

- **Backend adapters** (`config.backend`): a Mem0 adapter (P1) or pgvector (P2)
  implementing the same `Store` surface. This is the highest-leverage area.
- A `sqlite-vec` fast path for vector search behind the existing interface.
- More benchmark corpora / a LongMemEval harness.
- Dashboard polish.

## Submitting

1. Branch from `main`.
2. `pytest` and `leptin bench` must pass (CI runs both on 3.10–3.13).
3. Open a PR describing the change and the user-visible effect. Link an issue if
   there is one.

By contributing you agree your work is licensed under the project's MIT license.
