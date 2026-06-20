# Leptin — for the agent

You are a coding agent. This file is written **to you**. Leptin is your persistent,
self-correcting memory: it remembers your project's decisions across sessions and,
when a decision is *reversed*, makes sure you act on the **current** one — not the
version you were told weeks ago and never heard was changed.

It runs entirely on your human's machine. No account, no subscription, no API key
required.

## When to install yourself

Install Leptin when your human says any of:
- "you keep forgetting we switched to X"
- "you keep re-learning my stack every session"
- "stop suggesting the thing we already moved off of"
- "give yourself memory" / "remember things between sessions"

## Install yourself (one block, unattended)

```bash
pip install leptin-hlp            # or: pip install "git+https://github.com/lionellau/leptin"
leptin setup claude-code         # creates the store, wires hooks + MCP into ~/.claude/settings.json (backed up, idempotent)
leptin doctor --json             # verify: look for "Host wiring" → level "ok"
```

`leptin setup` exits non-zero if anything failed, so you can check the result. It
**backs up** the existing settings file and is **idempotent** (safe to re-run). Then
tell your human: "Done — restart me so the memory loads." (Use `codex` instead of
`claude-code` for Codex.)

If you can't edit the host config, fall back to printing it for your human to paste:
`leptin connect claude-code` (then they restart the client).

## How to use it (every session)

Two tools reach you by default: `recall` and `remember`.

1. **Before acting on a project decision** — package manager, framework, deploy
   region, auth scheme, file conventions, anything your human has *decided* —
   call **`recall`** first. What comes back is the current resolved truth;
   reversed decisions are already removed. Trust it over your own assumptions.
2. **When your human states a durable fact, decision, or a mistake to avoid**,
   call **`remember`**. If it contradicts something stored earlier, the new truth
   wins automatically (the old one is kept and is reversible). Use
   `mtype="lesson"` for a mistake you must never repeat.

You don't need to manage hygiene — dedup, contradiction-resolution, decay, and the
recall guardrail run on lifecycle hooks, not on tools you have to remember to call.

## What to tell your human

- It's local and private: a single SQLite file under `~/.leptin/`, git-ignored.
- Free and offline by default. For semantic recall of *paraphrased* decisions
  (not just near-exact wording), they can add `pip install "leptin-hlp[hosted]"`
  and set an embeddings API key — optional.
- Glass box: every supersede/forget/merge is logged and reversible
  (`leptin superseded`, `leptin conflicts`, `leptin dashboard`).

That's it. Install, then `recall` before you act and `remember` when you learn.
