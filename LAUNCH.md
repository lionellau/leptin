# Leptin launch playbook

> **⚠️ Strategy update (current): discovery before launch.** Leptin's positioning is
> **personal, local-first memory infrastructure for an individual's coding agent** —
> not a token-budget SaaS component. Before working this playbook, validate the pain
> with **3 real users** (see [the discovery kit](#0-discovery-first-do-this-before-launching)).
> A broad launch on an unvalidated pitch burns the one shot. The headlines below are
> kept for when you're ready, already reframed to the correctness / personal-infra angle.

A GTM plan for when discovery confirms the pain. Synthesized from how recent dev-tool /
AI repos (ripgrep, uv, ruff, zoxide, aider, mem0, fabric) launched and got discovered.

## 0. Discovery first (do this before launching)

The real unknown is whether anyone hits stale-decision recall badly enough to install
a tool. Find out before broadcasting:

1. **Dogfood it yourself** — you're the ICP. Run `leptin setup` on your own long-lived
   project for two weeks; catch one real reversed-decision moment. That's your most
   credible story.
2. **3 warm conversations** — devs running Claude Code/Codex on a long project. Ask:
   "ever had your agent confidently act on a decision you'd already reversed?" Show
   `leptin demo`. Success signal: ≥1 unprompted "yes, that bites me — and that's a fix."
3. Only then work the launch sections below.

> **When you do launch, the biggest lever is engagement in the first 2 hours.**
> Pre-mobilize your network before you post anywhere.

---

## 0. Pre-flight (all shippable now)

- [x] README with founder story, benchmark, comparison table, FAQ, visuals
- [x] LICENSE (MIT), CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, issue/PR templates
- [x] CI workflow (the green CI badge is the highest trust-per-byte asset)
- [x] Social-preview image (`assets/social-preview.png`, 1280×640)
- [x] Architecture diagram (`assets/architecture.svg`)
- [ ] **Terminal demo GIF** — `vhs assets/demo.tape` → `assets/demo.gif`, then add it
      to the top of the README. (Highest-ROI single asset; needs `vhs` installed.)
- [ ] **Publish to PyPI** so `pip install leptin-hlp` works for real:
      `python -m build && python -m twine upload dist/*`
- [ ] Repo **description** + **20 topics** set (see below)
- [ ] **Social preview** uploaded (Settings → Options → Social preview)
- [ ] GitHub **Release** cut from the `v1.0.0` tag (autofills from CHANGELOG)

### Repo description (copy-paste)
```
Personal, local-first memory infrastructure for your coding agent. Keeps long-term memory correct when decisions change — when you reverse a choice, the agent stops acting on the abandoned one. No account, no subscription; your agent can install it itself. Python, zero deps, MIT, offline-first.
```

### Topics (the 20 that matter — set all)
```
mcp  mcp-server  model-context-protocol  memory  agent-memory  ai-agent
ai-agents  llm-memory  claude-code  claude  llm  python  sqlite
token-optimization  context-window  coding-assistant  developer-tools
open-source  offline  self-tuning
```
`claude-memory` and `agent-memory` are low-competition topic pages where a new
repo can rank early; `mcp` / `mcp-server` are the high-traffic ones.

---

## 1. Week −1: load the spring

1. Write a **"How I built Leptin"** post (800–1,200 words, include the `leptin bench`
   output). Publish on **dev.to** first, cross-post to **Hashnode**. End with the repo link.
2. Make sure everything in Pre-flight is green (especially PyPI + CI badge + social preview).
3. **DM 15–20 people** who work on AI/LLM tooling. Ask for *feedback*, not stars:
   "Launching Leptin tomorrow — local memory for your coding agent that keeps reversed decisions out (Claude Code / Codex).
   Would love your honest take." Stars follow feedback.
4. Spend the week being a genuine contributor in **r/ClaudeAI** and **r/mcp**
   (answer questions, comment). This earns the right to post on launch day.

---

## 2. Day 0: launch (Mon–Wed, 12:00–17:00 UTC)

### Hacker News — `Show HN`
Title:
```
Show HN: Leptin – local memory for your coding agent that keeps reversed decisions out
```
Within 5 minutes, post a 200–300 word **founder comment**: why you built it
(your agent kept acting on decisions you'd reversed), what's technically novel
(graded contradiction-supersede + a recall guardrail that proves nothing useful
was dropped + agent-self-install), and one specific thing you want feedback on
(e.g. "offline detector is lexical — is the free tier good enough, or do you want
the opt-in semantic path?"). Respond to criticism with "fair point — here's the
tradeoff," never defensively.

### Reddit (post sequentially, 24h apart, be present to reply)
- **r/ClaudeAI** — "My coding agent kept acting on decisions I'd already reversed —
  I built local memory that fixes it." Run `leptin demo`; paste the before/after.
- **r/mcp** — technical angle: how the recall guardrail works (code example).
- **r/LocalLLaMA** — "offline, local-first memory for coding agents that keeps the current decision authoritative (no
  key, MIT, Python) — here's the benchmark."

### X / Twitter — thread (5 tweets) with the demo GIF
Opener:
```
Your coding agent keeps acting on decisions you already reversed (still suggesting
pnpm weeks after you switched to bun). I built Leptin: local memory that keeps the
CURRENT decision authoritative. No account, no key — your agent installs it itself. 🧬
```
Then: the problem → `leptin demo` (before/after) → `leptin bench` (naive serves stale 100%, Leptin 0%) → install + link.

### Discords
Anthropic (#show-and-tell), the MCP community, aider, LangChain. 3–4 sentences +
the benchmark block + link. Customize per community; don't copy-paste identically.

---

## 3. Week 1: permanent referral traffic (awesome-lists)

Submit PRs (entry text below). These compound for months.
```
[Leptin](https://github.com/lionellau/leptin) — Personal, local-first memory for your coding agent; keeps the current decision authoritative when you reverse one (the agent stops acting on the abandoned version). No account, agent-installable, Python, zero deps, offline-first, MIT.
```
Targets, by fit:
1. `wong2/awesome-mcp-servers` (submit via mcpservers.org/submit)
2. `appcypher/awesome-mcp-servers`
3. `abordage/awesome-mcp` (Memory & RAG category)
4. `punkpeye/awesome-mcp-servers`
5. `ai-boost/awesome-harness-engineering` (Memory & State — high precision fit)
6. `rohitg00/awesome-claude-code-toolkit`
7. `TeleAI-UAGI/Awesome-Agent-Memory`
8. `korchasa/awesome-mcp`
9. mcpservers.org/submit (aggregator)

Also submit to newsletters: **Console.dev** (console.dev/tools), **TLDR** (tldr.tech/ai).

---

## 4. Week 2–3: second wave

- **Product Hunt** launch (Tue/Wed). Pre-recruit makers to *comment* (not just upvote).
  Wait until you have ~50 GitHub stars so "X stars on GitHub" is credible.
- Reach out to **aider** / **mem0** maintainers to be listed as a compatible memory
  layer ("works alongside your existing stack"). Integration mentions = permanent traffic.
- Day-14 follow-up dev.to post: "What I learned running Leptin in production" +
  a real `leptin tune --history` output (shows the self-tuning feature is alive).

---

## 5. What actually drives 100 → 1,000

1. **GitHub Trending** — 30+ stars in a day can land you on `/trending?l=python`,
   which is self-reinforcing. The HN/Reddit launch day is your shot.
2. **SEO content** — dev.to/Hashnode posts targeting "coding agent stale memory", "agent forgets decisions",
   "Claude Code memory cost", "AI agent context window" drive steady inbound for months.
3. **Integrations & mentions** in other tools' docs/READMEs.
4. **"Leptin learned X"** posts sharing real self-tuning / savings-ledger output —
   the tool markets itself when it shows receipts.

---

## Title / post templates (copy-usable)

| Channel | Text |
|---|---|
| HN | `Show HN: Leptin – local memory for your coding agent that keeps reversed decisions out` |
| r/ClaudeAI | `I built a memory MCP server that shows you exactly what your memory layer costs you` |
| r/LocalLLaMA | `Leptin: local memory for coding agents that drops reversed decisions (no account, MIT, Python)` |
| Product Hunt | `Personal local memory for AI coding agents — keeps the current decision authoritative` |
| awesome-list | see entry above |
