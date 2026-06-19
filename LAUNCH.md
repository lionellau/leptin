# Leptin launch playbook

A concrete GTM plan to get Leptin from 0 → 1,000 GitHub stars. Synthesized from
how recent dev-tool / AI repos (ripgrep, uv, ruff, zoxide, aider, mem0, fabric)
launched and got discovered. Work top-to-bottom.

> **The single biggest lever is launch-day engagement in the first 2 hours.**
> Pre-mobilize your network (below) before you post anywhere.

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
Token-budgeted MCP memory server for AI coding agents. Cuts memory-layer token spend ≥60%, shows an auditable savings ledger, guarantees zero silent forgetting. Python, zero deps, MIT, offline-first.
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
   "Launching Leptin tomorrow — a token-budgeted MCP memory server for Claude Code.
   Would love your honest take." Stars follow feedback.
4. Spend the week being a genuine contributor in **r/ClaudeAI** and **r/mcp**
   (answer questions, comment). This earns the right to post on launch day.

---

## 2. Day 0: launch (Mon–Wed, 12:00–17:00 UTC)

### Hacker News — `Show HN`
Title:
```
Show HN: Leptin – MCP memory server that puts agent memory on a token budget
```
Within 5 minutes, post a 200–300 word **founder comment**: why you built it
(the cost problem), what's technically novel (recall guardrail + savings ledger +
self-tuning), and one specific thing you want feedback on (e.g. "offline hashing
embedder by default — would you prefer requiring sentence-transformers?").
Respond to criticism with "fair point — here's the tradeoff," never defensively.

### Reddit (post sequentially, 24h apart, be present to reply)
- **r/ClaudeAI** — "I built a memory MCP server that shows you exactly how many
  tokens and dollars your memory layer costs you." Paste the benchmark block.
- **r/mcp** — technical angle: how the recall guardrail works (code example).
- **r/LocalLLaMA** — "offline-first MCP memory with a hard token budget (no API
  key, MIT, Python) — here's the benchmark."

### X / Twitter — thread (5 tweets) with the demo GIF
Opener:
```
Your Claude Code memory MCP is billing you in the dark. I built Leptin to fix it:
token-budgeted recall, an auditable savings ledger, and rollback-safe compaction.
≥60% fewer tokens at 0% recall loss. Free, MIT, offline. 🧬
```
Then: the problem → the `leptin bench` block → the 4 mechanisms → install + link.

### Discords
Anthropic (#show-and-tell), the MCP community, aider, LangChain. 3–4 sentences +
the benchmark block + link. Customize per community; don't copy-paste identically.

---

## 3. Week 1: permanent referral traffic (awesome-lists)

Submit PRs (entry text below). These compound for months.
```
[Leptin](https://github.com/lionellau/leptin) — Token-budgeted MCP memory server for AI coding agents; ≥60% token reduction with an auditable savings ledger and rollback-safe guardrail. Python, zero deps, offline-first, MIT.
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
2. **SEO content** — the dev.to/Hashnode posts targeting "MCP memory token budget",
   "Claude Code memory cost", "AI agent context window" drive steady inbound for months.
3. **Integrations & mentions** in other tools' docs/READMEs.
4. **"Leptin learned X"** posts sharing real self-tuning / savings-ledger output —
   the tool markets itself when it shows receipts.

---

## Title / post templates (copy-usable)

| Channel | Text |
|---|---|
| HN | `Show HN: Leptin – MCP memory server that puts agent memory on a token budget` |
| r/ClaudeAI | `I built a memory MCP server that shows you exactly what your memory layer costs you` |
| r/LocalLLaMA | `Leptin: offline-first MCP memory with a hard token budget (no API key, MIT, Python)` |
| Product Hunt | `Token-budgeted MCP memory for AI agents — with auditable savings receipts` |
| awesome-list | see entry above |
