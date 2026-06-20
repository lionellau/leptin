# Why Leptin is a loop, not a tool

> Design note. The one-paragraph version lives in the [README](../README.md#how-it-works); this is the reasoning behind it.

## The mistake we started with

The obvious way to give an agent better memory is to ship an **MCP server** with a
rich tool surface — `remember`, `recall`, `dedup`, `supersede`, `decay`, `compact`,
`forget`, `tune` — and let the model orchestrate its own hygiene.

That doesn't work, for a structural reason: **MCP is *pull*.** A tool only runs when
the model decides to call it. But memory hygiene is exactly the kind of work a model
*won't* reliably choose to do — it's not what the user asked for this turn, it costs
tokens, and there's no immediate reward. You end up with the same outcome as no
hygiene at all, plus a tax: every tool you expose ships its JSON schema in **every
request**, whether or not it's ever called. Eight governance tools is a permanent
context cost for work the model should never be in charge of triggering.

So the hero of a memory system can't be the MCP surface. The hero has to be the
**loop**.

## The harness already has a loop

Every coding-agent harness — Claude Code, Codex — runs a lifecycle around each turn,
and exposes it as **hooks**:

| Hook | When it fires | What Leptin does |
|---|---|---|
| `SessionStart` | a session begins | **inject** never-decaying lessons + the current-truth set into context |
| `UserPromptSubmit` | each user turn | optionally inject memories relevant to the prompt |
| `PostToolUse` | after every tool call | **capture** failures into lessons the moment they happen |
| `PreCompact` / `Stop` | context fills / session ends | run the **guarded prune** (decay, merge, supersede) |

These are *push* points. They fire whether or not the model "remembers" to do
anything. That is the whole game: **discipline that can't be skipped belongs on the
harness's push points, not behind a tool the model has to pull.**

Leptin is the code that runs on those points. The MCP server still exists — but it's
deliberately tiny: just `recall` and `remember`, the two things the model genuinely
needs *in-band* while it works. Everything else (the governance) is the loop.

## The loop, stage by stage

```
        ┌──────────── 1 · inject (SessionStart) ────────────┐
        │   lessons + current truth → context               │
   6 · forget                                          2 · act
 (PreCompact/Stop)                                  (recall/remember)
   decay · prune noise                              the model works
        │                                                   │
   5 · verify ◄──── 4 · reconcile ◄──── 3 · capture (PostToolUse)
   (guardrail)      dedup · supersede     tool failure → lesson
   probe recall     current truth wins
   before any prune
```

1. **Inject.** At session start, the lessons-learned set (never decays) and the
   current-truth set are pushed into context. The agent begins the turn already
   knowing what it learned the hard way and what's true *now*.
2. **Act.** The model does its work, calling `recall`/`remember` as needed. These
   are the only model-facing tools.
3. **Capture.** When a tool call *genuinely* fails (a non-zero exit / stderr / an
   error field — not the word "error" appearing in benign output), the `PostToolUse`
   hook distills it into a **candidate** lesson. It re-injects but decays, and only
   *graduates* to permanent if it recurs — so the lesson corpus stays bounded.
4. **Reconcile.** On `remember`, near-duplicates merge and a *confident*
   contradiction **supersedes** the old fact (kept, marked, reversible, auditable —
   recall returns the current truth, not both). A contradiction the offline detector
   *can't confidently resolve* is **flagged for review**, not buried — keeping the
   conservative "never silently forget, never wrongly delete" guarantee. Offline the
   detector is lexical (negation / antonym / single-slot value-swap / numeric);
   semantic reversals need hosted embeddings.
5. **Verify.** Before any prune commits, Leptin re-runs a probe set against the
   post-prune store **inside a transaction**, checking the *fact* still resolves (not
   merely that an id survived). If recall would regress, the entire prune rolls back.
   The measure is only as sharp as the configured embedder (lexical offline), and the
   report says so (`low_confidence`, `verbatim_probe_fraction`).
6. **Forget.** What survives verification: cold facts decay below the floor and are
   pruned — reversibly, within the retention window. A strong, genuinely-used memory
   is never mistaken for noise; "noise" (injected a lot, never useful) simply isn't
   shielded from that decay-prune.

The loop closes: what's forgotten in stage 6 changes what's injected in stage 1 next
session, and the **usefulness signal** from stages 2–3 feeds stage 6's prune
decisions. Memory gets *more correct and more relevant* with use, not just bigger.

## Why this is the differentiator

A **store** (Mem0, a vector DB) is excellent at stage 2 — save and retrieve by
similarity. A **context compressor** (e.g. Headroom) is excellent at shrinking what
the agent reads each turn. Neither closes the loop: neither decides which of two
conflicting facts is *currently* true, proves a prune didn't cost you a fact, makes a
lesson permanent, or learns which memories earned their place.

That's the axis Leptin owns. It is not a better store and not a better compressor —
it's the **control loop** you wrap around whichever of those you already use, so the
long-term memory underneath stays correct and useful over weeks and months.

## Consequences of the design

- **Lean MCP surface.** Two tools by default (`LEPTIN_MCP_TOOLS=all` opts into the
  rest for power users / scripting). Minimal per-request schema tax.
- **Works with any store.** The loop operates on Leptin's own SQLite, but the
  pattern is store-agnostic; nothing stops you running it alongside another memory
  backend.
- **Local-first and auditable.** Every stage logs a reason; nothing is hard-deleted
  within the retention window. The loop is a glass box, not a black box.
- **No model cooperation required.** Because the discipline lives on hooks, it can't
  be skipped by a model that's busy doing something else — which is most of the time.

---

*See also: [the recall guardrail](../src/leptin/guardrail.py) (stage 5), the
[engine](../src/leptin/engine.py) (stages 4 & 6 + the usefulness flywheel), and
`leptin connect claude-code` (which wires stages 1, 3, and 6 into your harness).*
