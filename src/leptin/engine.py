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
import uuid
from typing import Any, Optional

from leptin.config import Config
from leptin.embeddings import Embedder, LocalHashingEmbedder, cosine, make_embedder
from leptin.guardrail import Guardrail, covers
from leptin.llm import Merger, detect_contradiction, make_merger
from leptin.storage import Store
from leptin.tokenizer import count_memory_tokens, count_tokens

_WORD = re.compile(r"[a-z0-9']+")
MAX_CONTENT_CHARS = 20_000  # guard against pathological inputs


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
        self.session_id = session_id or uuid.uuid4().hex
        self.session_start = self.store.now()
        # Offline mode → deterministic heuristic tokenizer; hosted → real tokenizer.
        self._offline = isinstance(self.embedder, LocalHashingEmbedder)
        self._tok_model = "heuristic" if self._offline else self.config.price_model

    # ------------------------------------------------------------------ utils
    def _tok(self, text: str) -> int:
        return count_tokens(text, self._tok_model)

    def _mtok(self, memories: list[dict[str, Any]]) -> int:
        return count_memory_tokens(memories, self._tok_model)

    def _embed(self, text: str) -> list[float]:
        """Embed with graceful degradation — never raises to the caller."""
        try:
            return self.embedder.embed(text)
        except Exception:
            # Hosted embedder unreachable: fall back to local so dedup/recall
            # keep working (the caller also tolerates an empty vector).
            try:
                if not self._offline:
                    self.embedder = LocalHashingEmbedder(self.config.embedding_dim)
                    self._offline = True
                    self._tok_model = "heuristic"
                    return self.embedder.embed(text)
            except Exception:
                pass
            return []

    def _decay_factor(self, last_accessed: float, now: float) -> float:
        half = self.config.decay_half_life_days
        if half <= 0:
            return 1.0
        days = max(0.0, (now - last_accessed) / 86400.0)
        return math.exp(-(math.log(2) / half) * days)

    def effective_strength(self, mem: dict[str, Any], now: Optional[float] = None) -> float:
        now = self.store.now() if now is None else now
        return float(mem["strength"]) * self._decay_factor(mem["last_accessed_at"], now)

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
            out.append(
                {"score": sim * strength, "sim": sim, "strength": strength, "memory": m}
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
        self, content: str, subject: Optional[str] = None, source: Optional[str] = None
    ) -> dict[str, Any]:
        content = (content or "").strip()
        if not content:
            return {"action": "skipped", "memory_id": None, "tokens_saved": 0,
                    "reason": "empty content"}
        if len(content) > MAX_CONTENT_CHARS:
            content = content[:MAX_CONTENT_CHARS]

        new_tokens = self._tok(content)
        emb = self._embed(content)

        # Subject-aware scoring against existing memories (None subject is its
        # own group). We only attempt dedup/supersede when we have an embedding.
        scored: list[tuple[float, dict[str, Any]]] = []
        if emb:
            for m in self.store.list_memories(status="active"):
                if m.get("subject") != subject:
                    continue
                scored.append((self._similarity(content, emb, m), m))
            scored.sort(key=lambda x: x[0], reverse=True)
        best_sim, best = (scored[0] if scored else (0.0, None))

        # 1) Near-duplicate (sim ≥ τ): merge, or supersede on contradiction.
        if best is not None and best_sim >= self.config.dedup_threshold:
            decision = self.merger.decide(best["content"], content, best_sim)
            if decision.action == "supersede":
                stale = self._contradicting(scored, content)
                return self._supersede(stale or [best], content, emb, new_tokens,
                                       subject, source, decision.reason)
            return self._merge(best, content, emb, new_tokens, decision.reason)

        # 2) Lower-similarity contradiction (same subject, conflicting facts):
        #    supersede every stale version, even if not lexically near-identical.
        stale = self._contradicting(scored, content)
        if stale:
            return self._supersede(stale, content, emb, new_tokens, subject, source,
                                   "newer fact contradicts existing memory")

        # 3) No duplicate → create.
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            source_session=self.session_id, provenance=source,
        )
        self.store.add_event(mem["id"], "create", reason=source or "new memory",
                             token_delta=new_tokens)
        self._log_footprint("remember", reduced=0,
                            detail={"action": "created", "memory_id": mem["id"]})
        return {"action": "created", "memory_id": mem["id"], "tokens_saved": 0}

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
        decision = self.merger.decide(best["content"], content, 1.0)
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

    def _supersede(self, olds, content, emb, new_tokens, subject, source, reason) -> dict[str, Any]:
        now = self.store.now()
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            strength=1.0, source_session=self.session_id, provenance=source,
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
        budget = int(token_budget or self.config.token_budget_default)
        k = int(k or self.config.recall_k)
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
        budget = int(budget or self.config.token_budget_default)
        k = int(k or self.config.recall_k)
        return self._pack(self._rank(query, now)[:k], budget)

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
        return self.guardrail.guarded_compact(dry_run=dry_run)

    def plan_compaction(self, now: float) -> dict[str, Any]:
        """Compute (but do not apply) the set of prune/merge actions."""
        actives = self.store.list_memories(status="active")
        decayed = []
        for m in actives:
            if self.effective_strength(m, now) < self.config.strength_floor:
                decayed.append(m)
        return {"decayed": decayed}

    def apply_compaction(self, plan: dict[str, Any], now: float) -> dict[str, Any]:
        """Apply a compaction plan in-place (caller manages the transaction)."""
        decayed_ids = []
        freed = 0
        until = now + self.config.reversible_window_days * 86400.0
        for m in plan["decayed"]:
            self.store.update_memory(m["id"], status="quarantined", reversible_until=until)
            self.store.add_event(m["id"], "decay",
                                 reason="strength below floor", token_delta=-m["tokens"])
            decayed_ids.append(m["id"])
            freed += m["tokens"]
        return {"decayed": decayed_ids, "merged": [], "superseded": [], "freed_tokens": freed}

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
        }

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
        }
