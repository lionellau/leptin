"""Reproducible benchmark: naive top-k store vs. Leptin.

Runs offline on a bundled, deterministic LoCoMo-style corpus (multi-session
dialog with the redundancy and contradictions real memory stores accumulate).
The headline is CORRECTNESS — after a decision is reversed, does the store still
serve the stale fact (naive: yes; Leptin: no, at 0% recall loss)? Token footprint
is reported too, split honestly into packing vs governance.

    leptin bench                  # default budget
    leptin bench --budget 200     # tune the recall token ceiling

The corpus is pinned in code so the numbers are reproducible without network or
API keys. With a real embedding/LLM model configured, the same harness measures
your hosted setup.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from leptin.config import Config
from leptin.engine import DietEngine
from leptin.storage import Store
from leptin.tokenizer import count_memory_tokens

FIXED_CLOCK = lambda: 1_700_000_000.0  # noqa: E731 - frozen time → deterministic

# --- Bundled corpus ----------------------------------------------------------
# Each base fact recurs across "sessions" (exact repeats + light paraphrases),
# mirroring how multi-session dialog re-states the same facts. A few facts are
# later contradicted to exercise supersede.
_BASE_FACTS: list[tuple[str, str, list[str]]] = [
    # (subject, canonical fact, [restatements across sessions])
    ("prefs", "The user prefers dark mode in every app.",
     ["The user prefers dark mode in every app.",
      "User likes dark mode in every app.",
      "The user prefers dark mode in every app."]),
    ("prefs", "The user wants concise answers with no preamble.",
     ["The user wants concise answers with no preamble.",
      "The user wants concise answers with no preamble.",
      "User asks for concise answers and no preamble."]),
    ("prefs", "The user codes mainly in Python and TypeScript.",
     ["The user codes mainly in Python and TypeScript.",
      "User mainly writes code in Python and TypeScript."]),
    ("prefs", "The user dislikes tabs and prefers spaces for indentation.",
     ["The user dislikes tabs and prefers spaces for indentation.",
      "The user dislikes tabs and prefers spaces for indentation."]),
    ("stack", "The backend is FastAPI running on Postgres.",
     ["The backend is FastAPI running on Postgres.",
      "Backend is FastAPI on a Postgres database.",
      "The backend is FastAPI running on Postgres."]),
    ("stack", "The frontend uses React with Vite and Tailwind.",
     ["The frontend uses React with Vite and Tailwind.",
      "Frontend uses React, Vite, and Tailwind."]),
    ("stack", "Deployments run on Fly.io via GitHub Actions.",
     ["Deployments run on Fly.io via GitHub Actions.",
      "Deploys run on Fly.io through GitHub Actions.",
      "Deployments run on Fly.io via GitHub Actions."]),
    ("stack", "Background jobs use Celery with a Redis broker.",
     ["Background jobs use Celery with a Redis broker.",
      "Background jobs use Celery and a Redis broker."]),
    ("people", "Alice is the lead designer on the team.",
     ["Alice is the lead designer on the team.",
      "Alice leads design on the team."]),
    ("people", "Bob manages the on-call rotation.",
     ["Bob manages the on-call rotation.",
      "Bob owns the on-call rotation.",
      "Bob manages the on-call rotation."]),
    ("people", "Carol is the product manager for the billing area.",
     ["Carol is the product manager for the billing area.",
      "Carol is the PM for the billing area."]),
    ("schedule", "Standup is at 9:30am Pacific every weekday.",
     ["Standup is at 9:30am Pacific every weekday.",
      "Daily standup happens at 9:30am Pacific on weekdays."]),
    ("schedule", "Release day is the last Thursday of each month.",
     ["Release day is the last Thursday of each month.",
      "Releases ship on the last Thursday of every month."]),
    ("infra", "Production secrets live in AWS Secrets Manager.",
     ["Production secrets live in AWS Secrets Manager.",
      "Prod secrets are stored in AWS Secrets Manager."]),
    ("infra", "Staging mirrors production but with seeded test data.",
     ["Staging mirrors production but with seeded test data.",
      "Staging is a copy of prod with seeded test data."]),
    ("infra", "Logs are shipped to Datadog with 30-day retention.",
     ["Logs are shipped to Datadog with 30-day retention.",
      "Logs go to Datadog and are kept for 30 days."]),
    ("api", "The public API is versioned under the /v2 prefix.",
     ["The public API is versioned under the /v2 prefix.",
      "The public API lives under the /v2 prefix."]),
    ("api", "Rate limiting is 100 requests per minute per key.",
     ["Rate limiting is 100 requests per minute per key.",
      "API rate limit is 100 requests per minute per key."]),
    ("testing", "Unit tests run with pytest and must stay above 85% coverage.",
     ["Unit tests run with pytest and must stay above 85% coverage.",
      "Pytest is used and coverage must stay above 85%."]),
    ("testing", "End-to-end tests run nightly with Playwright.",
     ["End-to-end tests run nightly with Playwright.",
      "Playwright e2e tests run every night."]),
]

# Contradictions on their own subjects (no overlap with base facts), so the
# newer fact cleanly supersedes the stale one. (subject, original, newer, q, expected)
_CONTRADICTIONS: list[tuple[str, str, str, str, str]] = [
    ("billing", "The free trial period is 14 days.",
     "The free trial period is now 30 days.",
     "how long is the free trial period", "30 days"),
    ("region", "The default deployment region is us-east-1.",
     "The default deployment region changed to us-west-2.",
     "what is the default deployment region now", "us-west-2"),
]

# Probe questions → expected substring that a correct recall must surface.
_PROBES: list[tuple[str, str]] = [
    ("what theme does the user prefer", "dark mode"),
    ("how does the user like answers formatted", "concise"),
    ("which languages does the user code in", "Python and TypeScript"),
    ("tabs or spaces for the user", "spaces"),
    ("what is the backend built with", "FastAPI"),
    ("what database does the backend use", "Postgres"),
    ("what does the frontend use", "React"),
    ("where do deployments run", "Fly.io"),
    ("how are background jobs processed", "Celery"),
    ("who is the lead designer", "Alice"),
    ("who manages on-call", "Bob"),
    ("who is the billing product manager", "Carol"),
    ("when is the standup", "9:30am"),
    ("when is release day", "last Thursday"),
    ("where are production secrets stored", "Secrets Manager"),
    ("what is in staging", "seeded test data"),
    ("where are logs shipped", "Datadog"),
    ("what prefix is the public api under", "/v2"),
    ("what is the api rate limit", "100 requests per minute"),
    ("what test runner is used", "pytest"),
    ("what coverage is required", "85%"),
    ("how do end to end tests run", "Playwright"),
]


def build_corpus() -> dict[str, Any]:
    inserts: list[tuple[str, str]] = []  # (subject, content) in session order
    for subject, _canonical, restatements in _BASE_FACTS:
        for r in restatements:
            inserts.append((subject, r))
    # Contradictions: original first, contradicting newer later.
    contra_probes = []
    for subject, original, newer, q, expected in _CONTRADICTIONS:
        inserts.insert(len(inserts) // 3, (subject, original))
        inserts.append((subject, newer))
        contra_probes.append((q, expected))
    probes = list(_PROBES) + contra_probes
    return {"inserts": inserts, "probes": probes}


def load_locomo(path: str, limit: int = 0) -> dict[str, Any]:
    """Load a real LoCoMo-format dataset into (inserts, probes).

    Handles the common LoCoMo JSON shapes defensively: a list of samples, each
    with a multi-session ``conversation`` (or ``sessions``) and ``qa`` pairs.
    Each dialogue turn becomes a memory (subject = speaker); each QA pair becomes
    a probe (question → answer).

    Note: meaningful recall on real LoCoMo needs **hosted embeddings** — the
    offline hashing embedder matches lexically, not semantically. Configure
    ``embedding_model`` (e.g. text-embedding-3-small) for real numbers.
    """
    import json

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    samples = data if isinstance(data, list) else data.get("samples") or data.get("data") or [data]
    if limit:
        samples = samples[:limit]

    inserts: list[tuple[str, str]] = []
    probes: list[tuple[str, str]] = []
    for i, sample in enumerate(samples):
        conv = sample.get("conversation") or sample.get("sessions") or {}
        sessions = conv.values() if isinstance(conv, dict) else conv
        for session in sessions:
            if not isinstance(session, list):
                continue
            for turn in session:
                if not isinstance(turn, dict):
                    continue
                text = turn.get("text") or turn.get("content") or turn.get("clean_text")
                if not text:
                    continue
                speaker = turn.get("speaker") or turn.get("role") or f"sample{i}"
                inserts.append((str(speaker), str(text)))
        for qa in (sample.get("qa") or sample.get("qas") or sample.get("questions") or []):
            if not isinstance(qa, dict):
                continue
            q = qa.get("question") or qa.get("q")
            a = qa.get("answer") or qa.get("a") or qa.get("expected")
            if q and a is not None:
                probes.append((str(q), str(a)))
    if not inserts or not probes:
        raise ValueError(f"no usable inserts/probes parsed from {path} "
                         f"(got {len(inserts)} inserts, {len(probes)} probes)")
    return {"inserts": inserts, "probes": probes}


def _make_engine(config: Config) -> tuple[Store, DietEngine]:
    store = Store(":memory:", clock=FIXED_CLOCK)
    engine = DietEngine(store, config)
    return store, engine


def _covered(memories: list[dict[str, Any]], expected: str) -> bool:
    exp = expected.lower()
    return any(exp in m["content"].lower() for m in memories)


# --- Correctness corpus: reversed decisions where the probe is lexically close
# to BOTH the stale and the current fact, so a naive store genuinely surfaces the
# wrong one. (subject, stale, current, probe, stale_marker, current_marker) ----
_REVERSALS: list[tuple[str, str, str, str, str, str]] = [
    ("pkg", "We use pnpm as our package manager.", "We use bun as our package manager.",
     "what package manager do we use", "pnpm", "bun"),
    ("trial", "The free trial period is 14 days.", "The free trial period is 30 days.",
     "how long is the free trial period", "14 days", "30 days"),
    ("region", "The default deploy region is us-east-1.", "The default deploy region is us-west-2.",
     "what is the default deploy region", "us-east-1", "us-west-2"),
    ("theme", "The user prefers dark mode.", "The user prefers light mode.",
     "what theme does the user prefer", "dark", "light"),
    ("db", "The primary database is MySQL.", "The primary database is Postgres.",
     "what is the primary database", "mysql", "postgres"),
    ("sessexp", "Sessions expire after 24 hours.", "Sessions expire after 1 hour.",
     "how long until sessions expire", "24 hours", "1 hour"),
    ("cache", "We cache with Redis.", "We cache with Memcached.",
     "what do we cache with", "redis", "memcached"),
    ("lang", "The service is written in Go.", "The service is written in Rust.",
     "what language is the service written in", "go", "rust"),
]


def run_correctness(embedding_model: str = "local-hash",
                    llm_model: str = "heuristic") -> dict[str, Any]:
    """Measure the actual wedge: after a decision is reversed, does the store still
    serve the STALE fact? A naive store (no supersede) does; Leptin shouldn't.

    Reports ``stale_fact_returned_rate`` (lower is better) and current-truth
    coverage for naive vs Leptin — the correctness number the token figure can't
    stand in for."""
    # Truly naive: BOTH dedup and supersede off (supersede is gated by
    # contradiction_threshold, not dedup_threshold) → it keeps the stale fact.
    naive_cfg = Config(dedup_threshold=2.0, contradiction_threshold=2.0,
                       embedding_model=embedding_model, llm_model=llm_model)
    lep_cfg = Config(embedding_model=embedding_model, llm_model=llm_model)

    def build(cfg: Config) -> tuple[Store, DietEngine]:
        store, eng = _make_engine(cfg)
        for subject, stale, current, *_ in _REVERSALS:
            eng.remember(stale, subject=subject)
            eng.remember(current, subject=subject)
        return store, eng

    def measure(eng: DietEngine) -> tuple[float, float]:
        wrong = surfaced = 0
        for _s, _stale, _cur, q, sm, cm in _REVERSALS:
            text = " ".join(m["content"].lower() for m in eng.recall(q)["memories"])
            if sm.lower() in text:
                wrong += 1              # served the stale fact (incorrect)
            if cm.lower() in text:
                surfaced += 1           # surfaced the current truth
        n = len(_REVERSALS)
        return wrong / n, surfaced / n

    ns, ne = build(naive_cfg)
    ls, le = build(lep_cfg)
    naive_stale, naive_current = measure(ne)
    lep_stale, lep_current = measure(le)
    ns.close(); ls.close()
    return {
        "n_reversals": len(_REVERSALS),
        "naive_stale_rate": round(naive_stale, 3),
        "leptin_stale_rate": round(lep_stale, 3),
        "naive_current_coverage": round(naive_current, 3),
        "leptin_current_coverage": round(lep_current, 3),
        "pass": lep_stale <= 0.1 and lep_stale < naive_stale and lep_current >= 0.9,
    }


def eval_contradiction(path: Optional[str] = None) -> dict[str, Any]:
    """Precision/recall/F1 of the offline contradiction detector against a labeled
    dataset (defaults to the bundled set). Honest about the offline ceiling:
    paraphrase reversals that need hosted embeddings show up as recall misses."""
    import json
    import os

    from leptin.llm import contradiction_signal

    path = path or os.path.join(os.path.dirname(__file__), "data", "contradictions.jsonl")
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    tp = fp = fn = tn = 0
    misses: list[str] = []
    for r in rows:
        gold = bool(r["contradiction"])
        pred = bool(contradiction_signal(r["a"], r["b"]).certain)
        if gold and pred:
            tp += 1
        elif gold and not pred:
            fn += 1
            misses.append(f"{r['a']!r} vs {r['b']!r}")
        elif not gold and pred:
            fp += 1
        else:
            tn += 1
    prec = tp / (tp + fp) if (tp + fp) else 1.0
    rec = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return {"n": len(rows), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3),
            "false_positives": fp, "recall_misses_needing_hosted": misses}


def run(budget: int = 1500, naive_top_k: int = 10, verbose: bool = False,
        corpus: Optional[dict[str, Any]] = None, embedding_model: str = "local-hash",
        llm_model: str = "heuristic") -> dict[str, Any]:
    corpus = corpus or build_corpus()
    inserts = corpus["inserts"]
    probes = corpus["probes"]

    # --- Naive store: no governance (dedup AND supersede off), no budget cap, no
    #     relevance gate; dumps top-k. The honest "unbudgeted top-k" baseline. ---
    naive_cfg = Config(dedup_threshold=2.0, contradiction_threshold=2.0,
                       token_budget_default=10**9,
                       recall_k=naive_top_k, naive_top_k=naive_top_k,
                       recall_rel_floor=0.0, recall_min_sim=0.0, offline_recall_min_sim=0.0,
                       embedding_model=embedding_model, llm_model=llm_model)
    naive_store, naive = _make_engine(naive_cfg)
    t0 = time.perf_counter()
    for subject, content in inserts:
        naive.remember(content, subject=subject)
    naive_insert_s = time.perf_counter() - t0

    # --- Leptin store: dedup/merge + budgeted packed recall. ---
    lep_cfg = Config(token_budget_default=budget, naive_top_k=naive_top_k,
                     embedding_model=embedding_model, llm_model=llm_model)
    lep_store, lep = _make_engine(lep_cfg)
    t0 = time.perf_counter()
    for subject, content in inserts:
        lep.remember(content, subject=subject)
    lep_insert_s = time.perf_counter() - t0
    lep.compact()  # guardrailed compaction (no-op prune on fresh corpus)

    naive_active = naive_store.count_memories("active")
    lep_active = lep_store.count_memories("active")

    # --- Per-probe recall + token accounting. ---
    naive_tokens = 0
    lep_tokens = 0
    naive_hits = 0
    lep_hits = 0
    t0 = time.perf_counter()
    for q, expected in probes:
        nres = naive.recall(q)
        naive_tokens += nres["tokens_used"]
        if _covered(nres["memories"], expected):
            naive_hits += 1
    naive_recall_s = time.perf_counter() - t0

    t0 = time.perf_counter()
    for q, expected in probes:
        lres = lep.recall(q)
        lep_tokens += lres["tokens_used"]
        if _covered(lres["memories"], expected):
            lep_hits += 1
    lep_recall_s = time.perf_counter() - t0

    # --- Gated baseline: SAME relevance floor + budget as Leptin, but dedup/
    #     supersede OFF. Isolates how much reduction is *packing* (budget+floor)
    #     vs *governance* (dedup/supersede/decay) — so the headline doesn't let
    #     the packing axis (which Headroom also does) stand in for the loop. ---
    gated_cfg = Config(token_budget_default=budget, naive_top_k=naive_top_k,
                       dedup_threshold=2.0, contradiction_threshold=2.0,
                       embedding_model=embedding_model, llm_model=llm_model)
    gated_store, gated = _make_engine(gated_cfg)
    for subject, content in inserts:
        gated.remember(content, subject=subject)
    gated_tokens = sum(gated.recall(q)["tokens_used"] for q, _ in probes)
    gated_store.close()
    packing_reduction = (naive_tokens - gated_tokens) / naive_tokens if naive_tokens else 0.0
    governance_reduction = (gated_tokens - lep_tokens) / gated_tokens if gated_tokens else 0.0

    n_probes = len(probes)
    naive_recall = naive_hits / n_probes
    lep_recall = lep_hits / n_probes
    reduction = (naive_tokens - lep_tokens) / naive_tokens if naive_tokens else 0.0
    recall_delta = naive_recall - lep_recall  # positive = leptin lost recall

    report = lep.diet_report("all")

    result = {
        "budget": budget,
        "naive_top_k": naive_top_k,
        "n_inserts": len(inserts),
        "n_probes": n_probes,
        "naive_active_memories": naive_active,
        "leptin_active_memories": lep_active,
        "naive_tokens": naive_tokens,
        "gated_tokens": gated_tokens,
        "leptin_tokens": lep_tokens,
        "token_reduction_pct": round(reduction * 100, 1),
        "packing_reduction_pct": round(packing_reduction * 100, 1),
        "governance_reduction_pct": round(governance_reduction * 100, 1),
        "naive_recall": round(naive_recall, 4),
        "leptin_recall": round(lep_recall, 4),
        "recall_loss_pct": round(recall_delta * 100, 2),
        "usd_saved": report["usd_saved"],
        "latency_ms": {
            "naive_insert": round(naive_insert_s * 1000, 1),
            "leptin_insert": round(lep_insert_s * 1000, 1),
            "naive_recall_avg": round(naive_recall_s / n_probes * 1000, 2),
            "leptin_recall_avg": round(lep_recall_s / n_probes * 1000, 2),
        },
        "models": {"embedding": lep_cfg.embedding_model, "llm": lep_cfg.llm_model,
                   "price_model": lep_cfg.price_model},
        "headline_pass": reduction >= 0.60 and recall_delta <= 0.02,
    }
    naive_store.close()
    lep_store.close()
    return result


def format_table(r: dict[str, Any]) -> str:
    pass_str = "PASS ✅" if r["headline_pass"] else "MISS ❌"
    lines = ["", "  Leptin benchmark — correctness first, then footprint (offline, deterministic)",
             "  " + "=" * 64]

    # --- Correctness (the wedge) leads. ---
    c = r.get("correctness")
    if c:
        cpass = "PASS ✅" if c["pass"] else "MISS ❌"
        lines += [
            f"  CORRECTNESS — after {c['n_reversals']} reversed decisions:",
            f"    stale fact served : naive {c['naive_stale_rate']*100:.0f}%"
            f"   leptin {c['leptin_stale_rate']*100:.0f}%   (lower is better)",
            f"    current truth     : naive {c['naive_current_coverage']*100:.0f}%"
            f"   leptin {c['leptin_current_coverage']*100:.0f}%",
            f"    HEADLINE          : {cpass}  a naive store serves the OUTDATED fact "
            f"{c['naive_stale_rate']*100:.0f}% of the time; Leptin {c['leptin_stale_rate']*100:.0f}%",
            "  " + "-" * 64,
        ]

    # --- Footprint, with the packing-vs-governance split made explicit. ---
    lines += [
        f"  corpus            : {r['n_inserts']} inserts, {r['n_probes']} probes",
        f"  active memories   : naive {r['naive_active_memories']:>4}   leptin {r['leptin_active_memories']:>4}"
        f"   (dedup kept {r['naive_active_memories'] - r['leptin_active_memories']} out)",
        f"  memory tokens     : naive {r['naive_tokens']:>6}   leptin {r['leptin_tokens']:>6}",
        f"  token reduction   : {r['token_reduction_pct']}%   (≈{r['packing_reduction_pct']}% packing"
        f" + {r['governance_reduction_pct']}% governance on top)",
        f"  recall            : naive {r['naive_recall']:.3f}   leptin {r['leptin_recall']:.3f}"
        f"   (loss {r['recall_loss_pct']}%, target ≤ 2%)",
        f"  est. $ saved      : ${r['usd_saved']}  (priced at {r['models']['price_model']})",
        "  " + "-" * 64,
        f"  HEADLINE          : {pass_str}  current-truth correctness + ≥60% leaner recall at ≤2% loss",
        f"  models            : embedding={r['models']['embedding']}, llm={r['models']['llm']}",
        "  packing           : budget + relevance floor (the axis a compressor also helps with)",
        "  governance        : dedup / supersede / decay (the correctness loop — Leptin's wedge)",
        "  baseline          : unbudgeted top-k recall over the same store (not 'what MCPs do')",
        f"  corpus            : {r['dataset']} (real LoCoMo dataset)" if r.get("dataset")
        else "  corpus            : bundled synthetic LoCoMo-style set (illustrative, offline)",
        "",
    ]
    return "\n".join(lines)


def main(budget: int = 1500, naive_top_k: int = 10, dataset: Optional[str] = None,
         limit: int = 0, embedding_model: str = "local-hash",
         llm_model: str = "heuristic") -> dict[str, Any]:
    corpus = load_locomo(dataset, limit=limit) if dataset else None
    r = run(budget=budget, naive_top_k=naive_top_k, corpus=corpus,
            embedding_model=embedding_model, llm_model=llm_model)
    if dataset:
        r["dataset"] = dataset
    else:
        # The correctness bench uses the bundled reversal corpus (not real LoCoMo).
        r["correctness"] = run_correctness(embedding_model, llm_model)
        r["headline_pass"] = r["headline_pass"] and r["correctness"]["pass"]
    print(format_table(r))
    return r
