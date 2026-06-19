"""Reproducible benchmark: naive top-k store vs. Leptin.

Runs offline on a bundled, deterministic LoCoMo-style corpus (multi-session
dialog with the redundancy and contradictions real memory stores accumulate).
Reports token reduction %, recall delta, latency, and $ — the headline claim.

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


def run(budget: int = 1500, naive_top_k: int = 10, verbose: bool = False,
        corpus: Optional[dict[str, Any]] = None, embedding_model: str = "local-hash",
        llm_model: str = "heuristic") -> dict[str, Any]:
    corpus = corpus or build_corpus()
    inserts = corpus["inserts"]
    probes = corpus["probes"]

    # --- Naive store: never merges, no budget cap, no relevance gate; dumps top-k. ---
    naive_cfg = Config(dedup_threshold=2.0, token_budget_default=10**9,
                       recall_k=naive_top_k, naive_top_k=naive_top_k,
                       recall_rel_floor=0.0, recall_min_sim=0.0,
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
        "leptin_tokens": lep_tokens,
        "token_reduction_pct": round(reduction * 100, 1),
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
    lines = [
        "",
        "  Leptin benchmark — naive top-k store vs. Leptin (offline, deterministic)",
        "  " + "-" * 64,
        f"  corpus            : {r['n_inserts']} inserts, {r['n_probes']} probes",
        f"  active memories   : naive {r['naive_active_memories']:>4}   leptin {r['leptin_active_memories']:>4}"
        f"   (dedup kept {r['naive_active_memories'] - r['leptin_active_memories']} out)",
        f"  recall budget     : {r['budget']} tokens   |   naive dumps top-{r['naive_top_k']}",
        "  " + "-" * 64,
        f"  memory tokens     : naive {r['naive_tokens']:>6}   leptin {r['leptin_tokens']:>6}",
        f"  TOKEN REDUCTION   : {r['token_reduction_pct']}%   (target ≥ 60%)",
        f"  recall            : naive {r['naive_recall']:.3f}   leptin {r['leptin_recall']:.3f}",
        f"  RECALL LOSS       : {r['recall_loss_pct']}%   (target ≤ 2%)",
        f"  est. $ saved      : ${r['usd_saved']}  (priced at {r['models']['price_model']})",
        f"  recall latency    : {r['latency_ms']['leptin_recall_avg']} ms/query (leptin)",
        "  " + "-" * 64,
        f"  HEADLINE          : {pass_str}  "
        f"≥60% fewer memory tokens at ≤2% recall loss",
        f"  models            : embedding={r['models']['embedding']}, llm={r['models']['llm']}",
        "  driven by         : budgeted, relevance-packed recall + write-time dedup",
        "  baseline          : a naive top-k dump (what stock memory MCPs do today)",
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
    print(format_table(r))
    return r
