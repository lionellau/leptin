"""The diet engine — Leptin's brain.

Implements the five mechanisms from the PRD:
  1. write-time dedup / merge / supersede
  2. time-decay forgetting (Ebbinghaus-style, access-boosted)
  3. budgeted, packed recall
  4. the savings ledger
  5. recall-guarded compaction (delegated to :mod:`leptin.guardrail`)

All side-effecting ranking goes through :meth:`_rank` so the guardrail can probe
the store read-only without touching the ledger or reinforcing strength.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from typing import Any, Optional

from leptin.config import Config
from leptin.embeddings import Embedder, LocalHashingEmbedder, cosine, make_embedder
from leptin.guardrail import Guardrail, covers
from leptin.llm import HeuristicMerger, Merger, detect_contradiction, make_merger
from leptin.logconf import get_logger, warn_once as _warn_once
from leptin.storage import Store
from leptin.tokenizer import count_memory_tokens, count_tokens
from leptin.tuner import Tuner

_WORD = re.compile(r"[a-z0-9']+")
MAX_CONTENT_CHARS = 20_000  # guard against pathological inputs
_log = get_logger("engine")

# Memory types and how fast each decays, as a multiple of decay_half_life_days.
# None = never decays. Lessons must persist; task notes fade with the task.
MEMORY_TYPES = ("fact", "procedural", "task", "lesson")
_TYPE_HALFLIFE_MULT: dict[str, Optional[float]] = {
    "fact": 1.0,        # facts/conventions — normal decay
    "procedural": 2.0,  # how-to/workflows — slow decay
    "task": 0.4,        # tied to a ticket — fades faster
    "lesson": None,     # lessons-learned / anti-patterns — never decay
}
_STALE_PENALTY = 0.25   # down-weight (don't hide) memories whose source changed


class DietEngine:
    def __init__(
        self,
        store: Store,
        config: Optional[Config] = None,
        embedder: Optional[Embedder] = None,
        merger: Optional[Merger] = None,
        session_id: Optional[str] = None,
    ):
        self.store = store
        self.config = config or Config()
        self.embedder = embedder or make_embedder(
            self.config.embedding_model, self.config.embedding_dim
        )
        self.merger = merger or make_merger(self.config.llm_model)
        self.guardrail = Guardrail(self)
        self.tuner = Tuner(self)
        self.session_id = session_id or uuid.uuid4().hex
        self.session_start = self.store.now()
        # Offline mode → deterministic heuristic tokenizer; hosted → real tokenizer.
        self._offline = isinstance(self.embedder, LocalHashingEmbedder)
        self._merger_offline = getattr(self.merger, "name", "") == "heuristic"
        self._tok_model = "heuristic" if self._offline else self.config.price_model
        # Hosted resilience + cost control.
        self._hosted_retries = 2          # retry transient API errors before downgrade
        self._retry_backoff = 0.05        # base seconds; exponential
        self._embed_cache: dict[str, list[float]] = {}  # text → vector (avoid re-billing)
        self._embed_cache_max = 2048

    # ------------------------------------------------------------------ utils
    def _tok(self, text: str) -> int:
        return count_tokens(text, self._tok_model)

    def _mtok(self, memories: list[dict[str, Any]]) -> int:
        return count_memory_tokens(memories, self._tok_model)

    def _embed(self, text: str) -> list[float]:
        """Embed with caching, transient-error retry, and graceful degradation.

        Never raises to the caller. For hosted embedders, a transient failure is
        retried with exponential backoff before downgrading to local — a single
        429/timeout shouldn't permanently lose semantic embeddings. Results are
        cached by text so repeated queries / dedup probes aren't re-billed.
        """
        cached = self._embed_cache.get(text)
        if cached is not None:
            return cached

        if self._offline:
            try:
                vec = self.embedder.embed(text)
            except Exception:
                return []
            self._cache_embed(text, vec)
            return vec

        last_exc: Optional[Exception] = None
        for attempt in range(self._hosted_retries + 1):
            try:
                vec = self.embedder.embed(text)
                self._cache_embed(text, vec)
                return vec
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._hosted_retries:
                    time.sleep(self._retry_backoff * (2 ** attempt))

        # All retries failed → downgrade to local, persistently.
        _warn_once(
            "embed-downgrade",
            f"embedding model '{self.config.embedding_model}' unavailable "
            f"({type(last_exc).__name__ if last_exc else 'error'}) after "
            f"{self._hosted_retries + 1} attempts; falling back to local-hash embeddings.",
        )
        self.embedder = LocalHashingEmbedder(self.config.embedding_dim)
        self._offline = True
        self._tok_model = "heuristic"
        self._embed_cache.clear()  # local vectors aren't comparable to hosted ones
        try:
            vec = self.embedder.embed(text)
            self._cache_embed(text, vec)
            return vec
        except Exception:
            return []

    def _cache_embed(self, text: str, vec: list[float]) -> None:
        if len(self._embed_cache) >= self._embed_cache_max:
            self._embed_cache.pop(next(iter(self._embed_cache)), None)  # FIFO evict
        self._embed_cache[text] = vec

    def _settle_embedder(self) -> None:
        """Force any pending hosted→local fallback to happen now, so a sequence of
        measurements (e.g. recall_before/after in a compaction) all use the same
        embedder and stay comparable."""
        if not self._offline:
            self._embed("warmup")  # triggers the fallback in _embed on failure

    def _safe_decide(self, older: str, newer: str, sim: float):
        """Merge/supersede decision with graceful degradation — never raises.

        If a hosted merger (LLM) is unreachable or its SDK is missing, fall back
        to the offline HeuristicMerger persistently (mirrors `_embed`). This is
        the PRD 8.1(d) edge case for the *merge* path.
        """
        if self._merger_offline:
            return self.merger.decide(older, newer, sim)
        last_exc: Optional[Exception] = None
        for attempt in range(self._hosted_retries + 1):
            try:
                return self.merger.decide(older, newer, sim)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._hosted_retries:
                    time.sleep(self._retry_backoff * (2 ** attempt))
        _warn_once(
            "merge-downgrade",
            f"merge model '{self.config.llm_model}' unavailable "
            f"({type(last_exc).__name__ if last_exc else 'error'}) after "
            f"{self._hosted_retries + 1} attempts; falling back to heuristic merge.",
        )
        self.merger = HeuristicMerger()
        self._merger_offline = True
        return self.merger.decide(older, newer, sim)

    def _decay_factor(self, last_accessed: float, now: float, half: float) -> float:
        if half <= 0:
            return 1.0
        days = max(0.0, (now - last_accessed) / 86400.0)
        return math.exp(-(math.log(2) / half) * days)

    def effective_strength(self, mem: dict[str, Any], now: Optional[float] = None) -> float:
        now = self.store.now() if now is None else now
        mult = _TYPE_HALFLIFE_MULT.get(mem.get("mtype", "fact"), 1.0)
        if mult is None:
            # Lessons-learned / anti-patterns never decay — they must stay
            # available so the agent stops repeating known mistakes.
            return float(mem["strength"])
        half = self.config.decay_half_life_days * mult
        return float(mem["strength"]) * self._decay_factor(mem["last_accessed_at"], now, half)

    def _keyword_sim(self, a: str, b: str) -> float:
        wa = set(_WORD.findall(a.lower()))
        wb = set(_WORD.findall(b.lower()))
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / math.sqrt(len(wa) * len(wb))

    def _similarity(self, query_text: str, query_emb: list[float], mem: dict[str, Any]) -> float:
        memb = mem.get("embedding") or []
        if query_emb and memb and len(query_emb) == len(memb):
            return cosine(query_emb, memb)
        # Degraded path: keyword overlap (recency/keyword recall).
        return self._keyword_sim(query_text, mem["content"])

    def _rank(self, query: str, now: Optional[float] = None) -> list[dict[str, Any]]:
        """Score all active memories for a query. Pure / read-only."""
        now = self.store.now() if now is None else now
        qemb = self._embed(query)
        out = []
        for m in self.store.list_memories(status="active"):
            sim = self._similarity(query, qemb, m)
            strength = self.effective_strength(m, now)
            score = sim * strength
            if m.get("stale"):
                score *= _STALE_PENALTY  # source changed — down-weight, don't hide
            out.append(
                {"score": score, "sim": sim, "strength": strength, "memory": m}
            )
        out.sort(key=lambda r: (r["score"], r["sim"]), reverse=True)
        return out

    def _reinforce(self, mem: dict[str, Any], now: float) -> None:
        eff = self.effective_strength(mem, now)
        new_strength = min(1.0, eff + self.config.access_boost)
        self.store.update_memory(
            mem["id"],
            strength=new_strength,
            last_accessed_at=now,
            access_count=mem["access_count"] + 1,
        )

    def _log_recall(
        self, baseline: int, actual: int, detail: Optional[dict[str, Any]] = None
    ) -> int:
        """Log a recall's *injection* savings — the headline savings currency.

        This is the one number that compounds honestly: every recall is a real,
        separate injection where Leptin really sent fewer tokens than a naive
        top-k dump would have.
        """
        saved = max(0, baseline - actual)
        self.store.add_ledger(
            operation="recall", baseline_tokens=baseline, actual_tokens=actual,
            tokens_saved=saved, model=self.config.price_model,
            usd_saved=self.config.usd_for_tokens(saved),
            session_id=self.session_id, detail=detail,
        )
        return saved

    def _log_footprint(
        self, operation: str, reduced: int, detail: dict[str, Any]
    ) -> int:
        """Log a one-time / reversible *store-footprint* reduction.

        Deliberately recorded with ``tokens_saved=0`` so it never inflates the
        headline injection-savings total or double-counts with future recalls.
        The reduction is preserved in ``detail.footprint_reduced`` and surfaced
        separately by :meth:`diet_report` as ``footprint_tokens_reduced``.
        """
        reduced = max(0, reduced)
        self.store.add_ledger(
            operation=operation, baseline_tokens=0, actual_tokens=0, tokens_saved=0,
            model=self.config.price_model, usd_saved=0.0,
            session_id=self.session_id,
            detail={**detail, "footprint_reduced": reduced},
        )
        return reduced

    # --------------------------------------------------------------- remember
    def remember(
        self, content: str, subject: Optional[str] = None, source: Optional[str] = None,
        mtype: str = "fact", source_ref: Optional[str] = None,
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"action": "skipped", "memory_id": None, "tokens_saved": 0,
                    "reason": "empty content"}
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS]
        if mtype not in MEMORY_TYPES:
            mtype = "fact"

        new_tokens = self._tok(content)
        emb = self._embed(content)

        # Dedup/supersede only against same subject AND same type, so a lesson
        # never merges into a fact (typing partitions the belief space).
        scored: list[tuple[float, dict[str, Any]]] = []
        if emb:
            for m in self.store.list_memories(status="active"):
                if m.get("subject") != subject or m.get("mtype", "fact") != mtype:
                    continue
                scored.append((self._similarity(content, emb, m), m))
            scored.sort(key=lambda x: x[0], reverse=True)
        best_sim, best = (scored[0] if scored else (0.0, None))

        # 1) Near-duplicate (sim ≥ τ): merge, or supersede on contradiction.
        if best is not None and best_sim >= self.config.dedup_threshold:
            decision = self._safe_decide(best["content"], content, best_sim)
            if decision.action == "supersede":
                stale = self._contradicting(scored, content)
                return self._supersede(stale or [best], content, emb, new_tokens,
                                       subject, source, decision.reason, mtype, source_ref)
            return self._merge(best, content, emb, new_tokens, decision.reason)

        # 2) Lower-similarity contradiction (same subject, conflicting facts):
        #    supersede every stale version, even if not lexically near-identical.
        stale = self._contradicting(scored, content)
        if stale:
            return self._supersede(stale, content, emb, new_tokens, subject, source,
                                   "newer fact contradicts existing memory", mtype, source_ref)

        # 3) No duplicate → create.
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            source_session=self.session_id, provenance=source,
            mtype=mtype, source_ref=source_ref,
        )
        self.store.add_event(mem["id"], "create", reason=source or "new memory",
                             token_delta=new_tokens)
        self._log_footprint("remember", reduced=0,
                            detail={"action": "created", "memory_id": mem["id"], "mtype": mtype})
        return {"action": "created", "memory_id": mem["id"], "tokens_saved": 0, "mtype": mtype}

    def _merge(self, best, content, emb, new_tokens, reason) -> dict[str, Any]:
        now = self.store.now()
        merged_content = (
            content if content == best["content"] else self._fuse(best, content)
        )
        merged_tokens = self._tok(merged_content)
        merged_emb = self._embed(merged_content)
        # Reinforce on merge (it's evidence the topic recurs).
        new_strength = min(1.0, self.effective_strength(best, now) + self.config.access_boost)
        self.store.update_memory(
            best["id"], content=merged_content, embedding=merged_emb,
            tokens=merged_tokens, strength=new_strength, last_accessed_at=now,
            access_count=best["access_count"] + 1,
        )
        reduced = (best["tokens"] + new_tokens) - merged_tokens
        saved = self._log_footprint("remember", reduced=reduced,
                                    detail={"action": "merged", "memory_id": best["id"]})
        self.store.add_event(best["id"], "merge", reason=reason, token_delta=-saved)
        return {"action": "merged", "memory_id": best["id"], "tokens_saved": saved}

    def _fuse(self, best, content) -> str:
        # Delegate to the merger's fusion (heuristic or hosted) for the canonical text.
        decision = self._safe_decide(best["content"], content, 1.0)
        return decision.content if decision.action == "merge" else content

    def _contradicting(
        self, scored: list[tuple[float, dict[str, Any]]], content: str
    ) -> list[dict[str, Any]]:
        """Same-subject active memories that conflict with new ``content``."""
        floor = self.config.contradiction_threshold
        return [
            m for sim, m in scored
            if sim >= floor and detect_contradiction(m["content"], content)
        ]

    def _supersede(self, olds, content, emb, new_tokens, subject, source, reason,
                   mtype: str = "fact", source_ref: Optional[str] = None) -> dict[str, Any]:
        now = self.store.now()
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            strength=1.0, source_session=self.session_id, provenance=source,
            mtype=mtype, source_ref=source_ref,
        )
        superseded_ids = []
        old_tokens = 0
        for o in olds:
            self.store.update_memory(o["id"], status="superseded", superseded_by=mem["id"])
            self.store.add_event(o["id"], "supersede", reason=reason, token_delta=-o["tokens"])
            superseded_ids.append(o["id"])
            old_tokens += o["tokens"]
        saved = self._log_footprint("remember", reduced=old_tokens,
                                    detail={"action": "superseded", "memory_id": mem["id"],
                                            "superseded": superseded_ids})
        self.store.add_event(mem["id"], "create",
                             reason="supersedes " + ", ".join(superseded_ids),
                             token_delta=new_tokens)
        return {"action": "superseded", "memory_id": mem["id"],
                "superseded": superseded_ids, "tokens_saved": saved}

    # ----------------------------------------------------------------- recall
    def recall(
        self, query: str, token_budget: Optional[int] = None, k: Optional[int] = None
    ) -> dict[str, Any]:
        query = (query or "").strip()
        # Treat only None as "unset" — a budget/k of 0 is an explicit ceiling,
        # not a fallback to the default (the falsy-zero bug).
        budget = int(self.config.token_budget_default if token_budget is None else token_budget)
        k = int(self.config.recall_k if k is None else k)
        budget = max(0, budget)
        k = max(0, k)
        now = self.store.now()

        ranked = self._rank(query, now)
        pool = ranked[:k]

        # Baseline = what a naive store dumps: its top-k *matches*. We only count
        # memories with non-zero similarity — a naive vector store would not
        # surface completely unrelated entries, so counting them would overstate
        # savings.
        by_sim = sorted((r for r in ranked if r["sim"] > 0),
                        key=lambda r: r["sim"], reverse=True)
        naive = [r["memory"] for r in by_sim[: self.config.naive_top_k]]
        baseline_tokens = self._mtok(naive)

        injected = self._pack(pool, budget)
        actual_tokens = self._mtok(injected)
        dropped = max(0, len(pool) - len(injected))

        for m in injected:
            self._reinforce(m, now)
            self.store.add_event(m["id"], "recall_inject", reason=f"query: {query[:60]}")

        saved = self._log_recall(
            baseline=baseline_tokens, actual=actual_tokens,
            detail={"query": query[:120], "injected": len(injected), "dropped": dropped},
        )

        return {
            "memories": [self._public_memory(m) for m in injected],
            "tokens_used": actual_tokens,
            "baseline_tokens": baseline_tokens,
            "tokens_saved": saved,
            "dropped_count": dropped,
            "budget": budget,
        }

    def _pack(self, pool: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
        """Greedy knapsack: pack the most relevant memories under the budget.

        A relevance gate drops marginally-on-topic memories (the padding a naive
        top-k dump would inject), so recall stays high while tokens stay low.
        """
        if not pool:
            return []
        best_sim = max((r["sim"] for r in pool), default=0.0)
        floor = max(self.config.recall_min_sim, best_sim * self.config.recall_rel_floor)
        injected: list[dict[str, Any]] = []
        for r in pool:
            if r["sim"] <= 0 or r["sim"] < floor:
                continue  # don't pad the budget with off-topic memories
            candidate = injected + [r["memory"]]
            if self._mtok(candidate) <= budget:
                injected = candidate
        return injected

    def _recall_preview(
        self, query: str, now: Optional[float] = None,
        budget: Optional[int] = None, k: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """The memories `recall` *would* inject — read-only, no ledger/reinforce.

        The guardrail measures recall against this (not a looser top-k), so the
        guardrail can never PASS while the real recall path would drop a fact.
        """
        now = self.store.now() if now is None else now
        budget = int(self.config.token_budget_default if budget is None else budget)
        k = int(self.config.recall_k if k is None else k)
        return self._pack(self._rank(query, now)[: max(0, k)], max(0, budget))

    def _live_id(self, memory_id: str) -> Optional[str]:
        """Follow the supersede chain to the memory that currently carries a fact."""
        seen: set[str] = set()
        cur = self.store.get_memory(memory_id)
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            nxt = cur.get("superseded_by")
            if not nxt:
                break
            following = self.store.get_memory(nxt)
            if not following:
                break
            cur = following
        return cur["id"] if cur else None

    # ---------------------------------------------------------------- compact
    def compact(self, dry_run: bool = False) -> dict[str, Any]:
        report = self.guardrail.guarded_compact(dry_run=dry_run)
        # Outer loop: opt-in self-tuning runs at the tail of a real compaction.
        if not dry_run and self.config.self_tune_enabled:
            try:
                should, trig = self.tuner.should_tune(self.store.now())
                if should:
                    report["tuning"] = self.tuner.tune(trigger=trig)
            except Exception as exc:  # never let tuning break compaction
                report["tuning_error"] = str(exc)
        return report

    def _recall_eval(self, query: str, now: Optional[float] = None) -> dict[str, int]:
        """Read-only recall metrics (actual vs naive-baseline tokens) for a query.
        Used by the tuner's evaluator; no side effects."""
        now = self.store.now() if now is None else now
        ranked = self._rank(query, now)
        injected = self._pack(ranked[: self.config.recall_k], self.config.token_budget_default)
        actual = self._mtok(injected)
        by_sim = sorted((r for r in ranked if r["sim"] > 0),
                        key=lambda r: r["sim"], reverse=True)
        baseline = self._mtok([r["memory"] for r in by_sim[: self.config.naive_top_k]])
        return {"actual_tokens": actual, "baseline_tokens": baseline}

    def plan_compaction(self, now: float) -> dict[str, Any]:
        """Compute (but do not apply) the set of prune/merge/supersede actions.

        - **decay**: active memories whose effective strength fell below the floor.
        - **merge**: leftover same-subject near-duplicates (sim ≥ τ) that slipped
          past write-time dedup — consolidate the weaker into the stronger.
        - **supersede**: same-subject contradictions still both active — the newer
          wins, the older is marked superseded.
        """
        actives = self.store.list_memories(status="active")
        decay_ids = {
            m["id"] for m in actives
            # Lessons never decay-prune (their effective_strength never drops),
            # but guard explicitly so they're never decay-eligible.
            if m.get("mtype") != "lesson"
            and self.effective_strength(m, now) < self.config.strength_floor
        }
        decayed = [m for m in actives if m["id"] in decay_ids]

        # Pairwise consolidation among the survivors (skip decay-eligible ones).
        survivors = [m for m in actives if m["id"] not in decay_ids]
        merges: list[tuple[dict, dict]] = []      # (winner, loser→merged-away)
        supersedes: list[tuple[dict, dict]] = []  # (newer-winner, older-loser)
        consumed: set[str] = set()
        by_subject: dict[Any, list[dict]] = {}
        for m in survivors:
            by_subject.setdefault(m.get("subject"), []).append(m)
        for group in by_subject.values():
            for i in range(len(group)):
                a = group[i]
                if a["id"] in consumed or not a.get("embedding"):
                    continue
                for j in range(i + 1, len(group)):
                    b = group[j]
                    if b["id"] in consumed or not b.get("embedding"):
                        continue
                    sim = self._similarity(a["content"], a["embedding"], b)
                    if sim < self.config.contradiction_threshold:
                        continue
                    if detect_contradiction(a["content"], b["content"]):
                        newer, older = (a, b) if a["created_at"] >= b["created_at"] else (b, a)
                        supersedes.append((newer, older))
                        consumed.add(older["id"])
                        if older is a:
                            break
                    elif sim >= self.config.dedup_threshold:
                        # Keep the stronger; merge the weaker's content into it.
                        keep, drop = (
                            (a, b) if self.effective_strength(a, now) >= self.effective_strength(b, now)
                            else (b, a)
                        )
                        merges.append((keep, drop))
                        consumed.add(drop["id"])
                        if drop is a:
                            break
        return {"decayed": decayed, "merges": merges, "supersedes": supersedes}

    def apply_compaction(self, plan: dict[str, Any], now: float) -> dict[str, Any]:
        """Apply a compaction plan in-place (caller manages the transaction)."""
        decayed_ids: list[str] = []
        merged_ids: list[str] = []
        superseded_ids: list[str] = []
        freed = 0
        until = now + self.config.reversible_window_days * 86400.0

        for m in plan["decayed"]:
            self.store.update_memory(m["id"], status="quarantined", reversible_until=until)
            self.store.add_event(m["id"], "decay",
                                 reason="strength below floor", token_delta=-m["tokens"])
            decayed_ids.append(m["id"])
            freed += m["tokens"]

        for keep, drop in plan.get("merges", []):
            fused = self._fuse(keep, drop["content"])
            fused_tokens = self._tok(fused)
            self.store.update_memory(keep["id"], content=fused,
                                     embedding=self._embed(fused), tokens=fused_tokens)
            self.store.update_memory(drop["id"], status="superseded", superseded_by=keep["id"])
            self.store.add_event(keep["id"], "merge",
                                 reason="compaction consolidated a near-duplicate",
                                 token_delta=-drop["tokens"])
            merged_ids.append(drop["id"])
            freed += drop["tokens"]

        for newer, older in plan.get("supersedes", []):
            self.store.update_memory(older["id"], status="superseded", superseded_by=newer["id"])
            self.store.add_event(older["id"], "supersede",
                                 reason="compaction resolved a contradiction",
                                 token_delta=-older["tokens"])
            superseded_ids.append(older["id"])
            freed += older["tokens"]

        return {"decayed": decayed_ids, "merged": merged_ids,
                "superseded": superseded_ids, "freed_tokens": freed}

    # ----------------------------------------------------------------- forget
    def forget(self, memory_id: Optional[str] = None, query: Optional[str] = None) -> dict[str, Any]:
        now = self.store.now()
        until = now + self.config.reversible_window_days * 86400.0
        targets: list[dict[str, Any]] = []
        if memory_id:
            m = self.store.get_memory(memory_id)
            if m and m["status"] == "active":
                targets = [m]
        elif query:
            ranked = self._rank(query, now)
            targets = [r["memory"] for r in ranked if r["sim"] >= 0.55][:10]
            if not targets and ranked and ranked[0]["sim"] > 0:
                targets = [ranked[0]["memory"]]
        forgotten = []
        freed = 0
        for m in targets:
            self.store.update_memory(m["id"], status="quarantined", reversible_until=until)
            self.store.add_event(m["id"], "forget", reason="user forget",
                                 token_delta=-m["tokens"])
            forgotten.append(self._public_memory(m))
            freed += m["tokens"]
        saved = 0
        if forgotten:
            # User-initiated removal is a footprint reduction, NOT credited as
            # Leptin's automatic savings (it would otherwise flatter the ledger).
            saved = self._log_footprint(
                "forget", reduced=freed,
                detail={"action": "forgotten", "count": len(forgotten),
                        "ids": [m["memory_id"] for m in forgotten]})
        return {"forgotten": forgotten, "reversible_until": until,
                "count": len(forgotten), "tokens_saved": saved}

    def restore(self, memory_id: str) -> dict[str, Any]:
        m = self.store.get_memory(memory_id)
        if not m:
            return {"restored": False, "reason": "not found"}
        if m["status"] == "active":
            return {"restored": False, "reason": "already active"}
        if m["status"] == "deleted":
            return {"restored": False, "reason": "purged after retention window"}
        now = self.store.now()
        self.store.update_memory(memory_id, status="active", last_accessed_at=now,
                                 superseded_by=None, reversible_until=None)
        self.store.add_event(memory_id, "restore", reason="user restore")
        return {"restored": True, "memory_id": memory_id}

    def add_probe(self, question: str, expected_fact: str) -> str:
        """Register a user guardrail probe, linking it to the memory it protects.

        Resolving ``source_memory_id`` lets the guardrail check by *identity*
        (did the protected memory survive?) rather than by lexical overlap — so
        an unrelated survivor sharing a token can't mask a real loss.
        """
        source_id = None
        for r in self._rank(question)[:5]:
            if covers(r["memory"]["content"], expected_fact):
                source_id = r["memory"]["id"]
                break
        return self.store.add_probe(question, expected_fact, source_memory_id=source_id)

    def purge_expired(self, now: Optional[float] = None) -> int:
        """Hard-expire quarantined memories past their reversible window."""
        now = self.store.now() if now is None else now
        expired = [
            m for m in self.store.list_memories(status="quarantined")
            if m.get("reversible_until") and m["reversible_until"] < now
        ]
        for m in expired:
            self.store.update_memory(m["id"], status="deleted")
            self.store.add_event(m["id"], "decay", reason="retention window elapsed")
        return len(expired)

    # ---------------------------------------------------------------- inspect
    def inspect(self, memory_id: Optional[str] = None, query: Optional[str] = None) -> dict[str, Any]:
        mem: Optional[dict[str, Any]] = None
        if memory_id:
            mem = self.store.get_memory(memory_id)
        elif query:
            ranked = self._rank(query)
            mem = ranked[0]["memory"] if ranked else None
        if not mem:
            return {"memory": None, "reason": "not found"}
        return {
            "memory": self._public_memory(mem),
            "provenance": {
                "source_session": mem.get("source_session"),
                "provenance": mem.get("provenance"),
                "created_at": mem.get("created_at"),
            },
            "strength": round(self.effective_strength(mem), 4),
            "status": mem["status"],
            "events": self.store.events_for(mem["id"]),
        }

    # ------------------------------------------------------------ diet_report
    def diet_report(self, window: str = "session") -> dict[str, Any]:
        now = self.store.now()
        if window == "session":
            rows = [r for r in self.store.ledger_rows()
                    if r.get("session_id") == self.session_id]
        elif window == "7d":
            rows = self.store.ledger_rows(since=now - 7 * 86400.0)
        else:
            rows = self.store.ledger_rows()

        # Headline savings = recall injection savings only (ongoing, real, never
        # double-counted). Footprint reductions (merge/supersede/decay/forget) are
        # one-time / reversible storage and reported separately.
        tokens_saved = sum(r["tokens_saved"] for r in rows)
        footprint_reduced = sum(int((r.get("detail") or {}).get("footprint_reduced", 0))
                                for r in rows)
        # Value savings at the *current* price model, consistent with the label.
        usd_saved = self.config.usd_for_tokens(tokens_saved)

        op_counts: dict[str, int] = {}
        action_counts = {"created": 0, "merged": 0, "superseded": 0, "decayed": 0,
                         "forgotten": 0}
        for r in rows:
            op = r["operation"]
            op_counts[op] = op_counts.get(op, 0) + 1
            detail = r.get("detail") or {}
            if op == "remember":
                action = detail.get("action")
                if action in action_counts:
                    action_counts[action] += 1
            elif op == "compact":
                action_counts["decayed"] += int(detail.get("decayed", 0))
            elif op == "forget":
                action_counts["forgotten"] += int(detail.get("count", 0))

        top = sorted(rows, key=lambda r: r["tokens_saved"], reverse=True)[:5]
        top_savers = [
            {"operation": r["operation"], "tokens_saved": r["tokens_saved"],
             "usd_saved": round(self.config.usd_for_tokens(r["tokens_saved"]), 6),
             "detail": r.get("detail")}
            for r in top if r["tokens_saved"] > 0
        ]

        last_run = self.store.conn.execute(
            "SELECT * FROM probe_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        guardrail_status = dict(last_run) if last_run else None

        note = None
        if tokens_saved == 0 and footprint_reduced == 0:
            note = ("No savings recorded yet — savings appear once memories "
                    "overlap (dedup/merge) or recall hits the token budget.")

        return {
            "window": window,
            "tokens_saved": tokens_saved,
            "usd_saved": round(usd_saved, 6),
            "footprint_tokens_reduced": footprint_reduced,
            "model": self.config.price_model,
            "ops": {**op_counts, **action_counts},
            "active_memories": self.store.count_memories("active"),
            "guardrail_status": guardrail_status,
            "top_savers": top_savers,
            "note": note,
            "tuning": self._tuning_report(),
        }

    def _tuning_report(self) -> Optional[dict[str, Any]]:
        """Self-tuning summary block (None until the tuner has run)."""
        return self.tuner.report() if getattr(self, "tuner", None) else None

    # ----------------------------------------------------- provenance / context
    def flag_stale(self, source_ref: str) -> dict[str, Any]:
        """Mark memories anchored to a source that changed as stale (not deleted).

        Stale memories are down-weighted in recall and surfaced for review — the
        answer to 'a fact is confidently wrong once its source changed'."""
        ids = self.store.flag_stale(source_ref)
        for mid in ids:
            self.store.add_event(mid, "decay", reason=f"source changed: {source_ref}")
        return {"flagged": ids, "count": len(ids), "source_ref": source_ref}

    def lessons(self) -> list[dict[str, Any]]:
        """All active lessons-learned / anti-patterns (never-decaying)."""
        return [self._public_memory(m)
                for m in self.store.list_memories(status="active")
                if m.get("mtype") == "lesson"]

    def session_context(self, query: Optional[str] = None,
                        token_budget: Optional[int] = None) -> dict[str, Any]:
        """What to inject at SessionStart: every lesson-learned, plus the most
        relevant memories for the query (if any), packed under a budget.

        This is the hook-injection surface — it is how memory reaches the model
        without the model having to call a tool."""
        now = self.store.now()
        budget = int(token_budget or self.config.token_budget_default)
        lessons = [m for m in self.store.list_memories(status="active")
                   if m.get("mtype") == "lesson"]
        injected = list(lessons)
        if query:
            ranked = self._rank(query, now)
            for r in ranked:
                if r["sim"] <= 0 or r["memory"].get("mtype") == "lesson":
                    continue
                candidate = injected + [r["memory"]]
                if self._mtok(candidate) <= budget:
                    injected.append(r["memory"])
        return {
            "lessons": [self._public_memory(m) for m in lessons],
            "memories": [self._public_memory(m) for m in injected if m.get("mtype") != "lesson"],
            "text": self._format_context(lessons, [m for m in injected if m.get("mtype") != "lesson"]),
            "tokens": self._mtok(injected),
        }

    def _format_context(self, lessons: list[dict[str, Any]],
                        memories: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        if lessons:
            lines.append("Lessons learned (do not repeat these):")
            lines += [f"- {m['content']}" for m in lessons]
        if memories:
            if lines:
                lines.append("")
            lines.append("Relevant memory:")
            lines += [f"- {(m.get('subject') + ': ') if m.get('subject') else ''}{m['content']}"
                      + (" [stale]" if m.get("stale") else "") for m in memories]
        return "\n".join(lines)

    # ------------------------------------------------------------------ views
    def _public_memory(self, mem: dict[str, Any]) -> dict[str, Any]:
        return {
            "memory_id": mem["id"],
            "subject": mem.get("subject"),
            "content": mem["content"],
            "tokens": mem["tokens"],
            "strength": round(self.effective_strength(mem), 4),
            "status": mem["status"],
            "access_count": mem["access_count"],
            "mtype": mem.get("mtype", "fact"),
            "source_ref": mem.get("source_ref"),
            "stale": bool(mem.get("stale")),
        }
