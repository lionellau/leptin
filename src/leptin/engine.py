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
from leptin.llm import HeuristicMerger, Merger, contradiction_signal, detect_contradiction, make_merger
from leptin.logconf import get_logger, warn_once as _warn_once
from leptin.storage import Store
from leptin.tokenizer import count_memory_tokens, count_tokens
from leptin.tuner import Tuner

_WORD = re.compile(r"[a-z0-9']+")
MAX_CONTENT_CHARS = 20_000  # guard against pathological inputs
_log = get_logger("engine")

# Memory types. How fast each decays (as a multiple of decay_half_life_days) and
# the noise/penalty knobs all live in Config now (see config.py) so they're
# inspectable, overridable, and locked against the self-tuner. ``lesson`` never
# decays — except an un-graduated AUTO-captured candidate lesson, which decays on
# ``candidate_lesson_half_life_days`` so a noisy auto-corpus self-prunes.
MEMORY_TYPES = ("fact", "procedural", "task", "lesson")
_AUTO_LESSON_SOURCE = "auto-captured"


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
        # A hosted outage now degrades to local for the failing call + a cooldown,
        # then retries hosted — instead of permanently pinning the store to local.
        self._hosted_cooldown_s = 300.0
        self._hosted_cooldown_until = 0.0
        self._fallback_local: Optional[LocalHashingEmbedder] = None
        self._last_local = self._offline  # did the most recent embed use local?

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
            self._last_local = True
            try:
                vec = self.embedder.embed(text)
            except Exception:
                return []
            self._cache_embed(text, vec)
            return vec

        # In a post-failure cooldown → use local for now, retry hosted later.
        now = self.store.now()
        if self._hosted_cooldown_until and now < self._hosted_cooldown_until:
            return self._embed_local(text)

        last_exc: Optional[Exception] = None
        for attempt in range(self._hosted_retries + 1):
            try:
                vec = self.embedder.embed(text)
                self._last_local = False
                self._cache_embed(text, vec)
                return vec
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt < self._hosted_retries:
                    time.sleep(self._retry_backoff * (2 ** attempt))

        # A missing SDK won't recover without an install → pin to local permanently.
        # A transient outage (timeout / 429 / connection) → local for THIS call +
        # a cooldown, then retry hosted: one blip shouldn't durably degrade a
        # semantic store to lexical.
        if isinstance(last_exc, (ImportError, ModuleNotFoundError)):
            _warn_once(
                "embed-downgrade",
                f"embedding SDK for '{self.config.embedding_model}' is not installed "
                f"({type(last_exc).__name__}); using local-hash embeddings. "
                f"`pip install leptin-hlp[hosted]` then `leptin reembed` to recover.",
            )
            self.embedder = LocalHashingEmbedder(self.config.embedding_dim)
            self._offline = True
            self._tok_model = "heuristic"
            self._embed_cache.clear()
            return self._embed_local(text)
        _warn_once(
            "embed-downgrade",
            f"embedding model '{self.config.embedding_model}' unavailable "
            f"({type(last_exc).__name__ if last_exc else 'error'}) after "
            f"{self._hosted_retries + 1} attempts; using local-hash for now and "
            f"retrying hosted after {int(self._hosted_cooldown_s)}s. "
            f"Run `leptin reembed` once it recovers to re-vectorise.",
        )
        self._hosted_cooldown_until = now + self._hosted_cooldown_s
        return self._embed_local(text)

    def _embed_local(self, text: str) -> list[float]:
        """Embed with the local hashing embedder (the hosted-outage fallback).
        Not written into the hosted cache (incomparable dims)."""
        if self._fallback_local is None:
            self._fallback_local = LocalHashingEmbedder(self.config.embedding_dim)
        self._last_local = True
        try:
            return self._fallback_local.embed(text)
        except Exception:
            return []

    def _cache_embed(self, text: str, vec: list[float]) -> None:
        if len(self._embed_cache) >= self._embed_cache_max:
            self._embed_cache.pop(next(iter(self._embed_cache)), None)  # FIFO evict
        self._embed_cache[text] = vec

    def reembed(self) -> dict[str, Any]:
        """Re-embed all active memories with the CURRENT embedder — recovery from a
        past hosted→local downgrade once the hosted model is back. Transactional;
        clears the cooldown so the next call tries hosted again."""
        self._hosted_cooldown_until = 0.0
        self._embed_cache.clear()
        actives = self.store.list_memories(status="active")
        self.store.begin()
        n = 0
        tag = self._embedder_tag([])
        try:
            for m in actives:
                vec = self._embed(m["content"])
                tag = self._embedder_tag(vec)
                self.store.update_memory(m["id"], embedding=vec, embedder=tag)
                n += 1
            self.store.commit()
        except Exception:
            self.store.rollback()
            raise
        return {"reembedded": n, "embedder": tag}

    def _settle_embedder(self) -> None:
        """Warm the embedder so a sequence of measurements (e.g. recall_before/
        after in a compaction) all use the same one and stay comparable."""
        if not self._offline:
            self._embed("warmup")  # triggers cooldown fallback in _embed on failure

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

    def _embedder_tag(self, emb: Optional[list[float]] = None) -> str:
        """Provenance of a vector this engine wrote, e.g. ``local-hash:256`` or
        ``text-embedding-3-small:1536`` — so a hosted→local downgrade is detectable
        per-row rather than silently mixing incomparable vectors. Reflects whether
        the most recent embed actually used local (cooldown fallback) and the
        vector's true dimensionality."""
        local = self._offline or self._last_local
        name = "local-hash" if local else self.config.embedding_model
        dim = len(emb) if emb else self.config.embedding_dim
        return f"{name}:{dim}"

    def _decay_factor(self, last_accessed: float, now: float, half: float) -> float:
        if half <= 0:
            return 1.0
        days = max(0.0, (now - last_accessed) / 86400.0)
        return math.exp(-(math.log(2) / half) * days)

    def _halflife_mult(self, mtype: str) -> float:
        if mtype == "procedural":
            return self.config.procedural_halflife_mult
        if mtype == "task":
            return self.config.task_halflife_mult
        return 1.0  # fact

    def _is_candidate_lesson(self, mem: dict[str, Any]) -> bool:
        """An auto-captured lesson that hasn't yet *graduated* (recurred across a
        session or earned explicit 'useful' feedback). Hand-authored lessons are
        never candidates; graduated ones become permanent."""
        return (mem.get("mtype") == "lesson"
                and mem.get("provenance") == _AUTO_LESSON_SOURCE
                and int(mem.get("useful_count", 0) or 0) == 0
                and int(mem.get("recur_sessions", 0) or 0) == 0)

    def effective_strength(self, mem: dict[str, Any], now: Optional[float] = None) -> float:
        now = self.store.now() if now is None else now
        # Outcome feedback: memories marked harmful are down-weighted (a wrong
        # memory that misled the agent should fade from recall).
        penalty = self.config.harmful_penalty ** int(mem.get("harmful_count", 0) or 0)
        base = float(mem["strength"])
        if mem.get("mtype") == "lesson":
            # Hand-authored / graduated lessons never decay — they must stay
            # available so the agent stops repeating known mistakes. An
            # un-graduated auto-captured candidate decays so noise self-prunes.
            half = self.config.candidate_lesson_half_life_days
            if self._is_candidate_lesson(mem) and half > 0:
                return base * self._decay_factor(mem["last_accessed_at"], now, half) * penalty
            return base * penalty
        half = self.config.decay_half_life_days * self._halflife_mult(mem.get("mtype", "fact"))
        return base * self._decay_factor(mem["last_accessed_at"], now, half) * penalty

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

    def _rank(self, query: str, now: Optional[float] = None,
              actives: Optional[list[dict[str, Any]]] = None) -> list[dict[str, Any]]:
        """Score active memories for a query. Pure / read-only.

        ``actives`` lets a caller (e.g. the guardrail measuring many probes) pass
        one snapshot so the active set isn't re-listed per call. With
        ``rank_candidate_limit > 0``, a cheap keyword prefilter trims the set
        before the full cosine scan — a scale guard for large stores."""
        now = self.store.now() if now is None else now
        qemb = self._embed(query)
        rows = self.store.list_memories(status="active") if actives is None else actives
        rows = self._prefilter(query, rows)
        out = []
        for m in rows:
            sim = self._similarity(query, qemb, m)
            strength = self.effective_strength(m, now)
            score = sim * strength
            if m.get("stale"):
                score *= self.config.stale_penalty  # source changed — down-weight, don't hide
            out.append(
                {"score": score, "sim": sim, "strength": strength, "memory": m}
            )
        # Recurrence is a *weak* tiebreaker only (a memory needed again across
        # sessions ranks above an equally-scored one that wasn't) — never a
        # primary signal, so it can't promote noise.
        out.sort(key=lambda r: (r["score"], r["sim"],
                                int(r["memory"].get("recur_sessions", 0) or 0)),
                 reverse=True)
        return out

    def _prefilter(self, query: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Optional O(n) keyword prefilter to cap the cosine scan at scale. Exact
        by default (limit 0). When trimming, keeps the rows with the most query-word
        overlap (ties keep insertion order) so on-topic memories aren't dropped."""
        limit = self.config.rank_candidate_limit
        if not limit or len(rows) <= limit:
            return rows
        qwords = set(_WORD.findall(query.lower()))
        if not qwords:
            return rows[:limit]
        scored = sorted(
            rows, key=lambda m: len(qwords & set(_WORD.findall(m["content"].lower()))),
            reverse=True,
        )
        return scored[:limit]

    def _reinforce(self, mem: dict[str, Any], now: float) -> None:
        eff = self.effective_strength(mem, now)
        new_strength = min(1.0, eff + self.config.access_boost)
        self.store.update_memory(
            mem["id"],
            strength=new_strength,
            last_accessed_at=now,
            access_count=mem["access_count"] + 1,
        )

    def _track_injection(self, mem: dict[str, Any], now: float) -> None:
        """Observe an injection (recall OR session-start push).

        Counts the injection and records *recurrence* — needed again in a later
        session, or again after a cooldown gap — as ``recur_sessions``. Recurrence
        is a WEAK ranking signal, deliberately NOT ``useful_count``: a note that
        merely keeps matching a recurring query isn't proven helpful, so it must
        not become un-prunable or guardrail-protected on recurrence alone.
        ``useful_count`` is reserved for explicit 'useful' feedback (it helped)."""
        last_session = mem.get("last_inject_session")
        last_at = mem.get("last_inject_at")
        fields: dict[str, Any] = {
            "inject_count": int(mem.get("inject_count", 0) or 0) + 1,
            "last_inject_session": self.session_id,
            "last_inject_at": now,
        }
        recurred = bool(last_session and last_session != self.session_id)
        if not recurred and last_at and (now - float(last_at)) >= self.config.recur_cooldown_seconds:
            recurred = True  # needed again after a gap, even within one long session
        if recurred:
            fields["recur_sessions"] = int(mem.get("recur_sessions", 0) or 0) + 1
        self.store.update_memory(mem["id"], **fields)

    def record_feedback(self, memory_ids: list[str], signal: str) -> dict[str, Any]:
        """Close the loop with an explicit outcome signal on recalled memories.

        ``useful`` reinforces and *reverses* one prior 'harmful' mark (so a noisy
        downvote is recoverable). ``harmful`` down-weights immediately, but only
        flags the memory stale + drops it from the guardrail's protected set once
        it crosses ``harmful_stale_threshold`` — a single noisy/adversarial signal
        shouldn't both cripple recall and blind the safety net."""
        now = self.store.now()
        touched = []
        thr = self.config.harmful_stale_threshold
        for mid in memory_ids:
            m = self.store.get_memory(mid)
            if not m:
                continue
            if signal == "useful":
                updates: dict[str, Any] = {"useful_count": int(m.get("useful_count", 0) or 0) + 1}
                h = int(m.get("harmful_count", 0) or 0)
                if h > 0:
                    updates["harmful_count"] = h - 1  # a 'useful' reverses a prior harmful mark
                    if h - 1 < thr:
                        updates["stale"] = 0  # lift the harmful-induced stale flag
                self.store.update_memory(mid, **updates)
                self._reinforce(self.store.get_memory(mid), now)
                self.store.add_event(mid, "recall_inject", reason="feedback: useful")
            elif signal == "harmful":
                h = int(m.get("harmful_count", 0) or 0) + 1
                updates = {"harmful_count": h}
                if h >= thr:
                    updates["stale"] = 1  # repeated harm → surface for review too
                self.store.update_memory(mid, **updates)
                self.store.add_event(mid, "decay", reason=f"feedback: harmful (x{h})")
            touched.append(mid)
        return {"signal": signal, "updated": touched, "count": len(touched)}

    def capture_lesson(self, content: str, subject: str = "anti-pattern") -> dict[str, Any]:
        """Auto-capture an anti-pattern as a *candidate* lesson — re-injected, but
        decaying (see :meth:`_is_candidate_lesson`) until it graduates by recurring
        or earning explicit 'useful' feedback. The mistake→lesson→prevent loop,
        closed automatically from a failure, without an unbounded permanent corpus."""
        result = self.remember(content, subject=subject, source=_AUTO_LESSON_SOURCE,
                               mtype="lesson")
        self._cap_auto_lessons()
        return result

    def _cap_auto_lessons(self) -> None:
        """Keep the auto-captured candidate corpus bounded: past ``max_auto_lessons``,
        quarantine (reversibly) the weakest un-graduated candidates. Hand-authored
        and graduated lessons are exempt."""
        now = self.store.now()
        cands = [m for m in self.store.list_memories(status="active")
                 if self._is_candidate_lesson(m)]
        if len(cands) <= self.config.max_auto_lessons:
            return
        cands.sort(key=lambda m: self.effective_strength(m, now))  # weakest first
        until = now + self.config.reversible_window_days * 86400.0
        for m in cands[: len(cands) - self.config.max_auto_lessons]:
            self.store.update_memory(m["id"], status="quarantined", reversible_until=until)
            self.store.add_event(m["id"], "decay",
                                 reason="auto-lesson cap: weakest candidate retired (reversible)")

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
        # never merges into a fact (typing partitions the belief space). Scoped to
        # the subject in SQL (NULL-safe) so ingestion is O(subject), not O(store).
        scored: list[tuple[float, dict[str, Any]]] = []
        if emb:
            for m in self.store.list_active_in_subject(subject):
                if m.get("mtype", "fact") != mtype:
                    continue
                scored.append((self._similarity(content, emb, m), m))
            scored.sort(key=lambda x: x[0], reverse=True)
        best_sim, best = (scored[0] if scored else (0.0, None))

        # 1) Near-duplicate (sim ≥ τ): merge, or supersede on a CERTAIN contradiction.
        if best is not None and best_sim >= self.config.dedup_threshold:
            decision = self._safe_decide(best["content"], content, best_sim)
            if decision.action == "supersede":
                stale = self._contradicting(scored, content)
                return self._supersede(stale or [best], content, emb, new_tokens,
                                       subject, source, decision.reason, mtype, source_ref)
            return self._merge(best, content, emb, new_tokens, decision.reason)

        # 2) Lower-similarity CERTAIN contradiction (same subject, confidently
        #    conflicting facts): supersede every stale version.
        stale = self._contradicting(scored, content)
        if stale:
            return self._supersede(stale, content, emb, new_tokens, subject, source,
                                   "newer fact contradicts existing memory", mtype, source_ref)

        # 3) No confident duplicate/contradiction → create. Then flag any
        #    UNCERTAIN same-subject conflicts for review — keep both active, never
        #    silently bury a true fact on a low-confidence signal.
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            source_session=self.session_id, provenance=source,
            mtype=mtype, source_ref=source_ref, embedder=self._embedder_tag(emb),
        )
        self.store.add_event(mem["id"], "create", reason=source or "new memory",
                             token_delta=new_tokens)
        conflict_id = self._flag_conflicts(scored, content, mem)
        self._log_footprint("remember", reduced=0,
                            detail={"action": "created", "memory_id": mem["id"], "mtype": mtype})
        out = {"action": "created", "memory_id": mem["id"], "tokens_saved": 0, "mtype": mtype}
        if conflict_id:
            out["conflicts_with"] = conflict_id  # surfaced for review (both kept)
        return out

    def _flag_conflicts(self, scored: list[tuple[float, dict[str, Any]]],
                        content: str, new_mem: dict[str, Any]) -> Optional[str]:
        """Link a same-subject memory that *may* contradict ``content`` (uncertain
        signal) for human review — non-destructive, both stay active."""
        floor = self.config.contradiction_threshold
        for sim, m in scored:
            if sim < floor:
                continue
            sig = contradiction_signal(m["content"], content)
            if sig.uncertain and not sig.certain:
                self.store.update_memory(m["id"], conflicts_with=new_mem["id"])
                self.store.update_memory(new_mem["id"], conflicts_with=m["id"])
                self.store.add_event(m["id"], "conflict",
                                     reason=f"possible conflict with newer memory — {sig.reason}")
                self.store.add_event(new_mem["id"], "conflict",
                                     reason=f"possible conflict with existing memory — {sig.reason}")
                return m["id"]
        return None

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
        until = now + self.config.reversible_window_days * 86400.0
        mem = self.store.add_memory(
            content=content, embedding=emb, tokens=new_tokens, subject=subject,
            strength=1.0, source_session=self.session_id, provenance=source,
            mtype=mtype, source_ref=source_ref, embedder=self._embedder_tag(emb),
        )
        superseded_ids = []
        old_tokens = 0
        for o in olds:
            # Reversible window: a write-time supersede is intentional truth-
            # replacement, but restorable (and discoverable via `list_superseded`)
            # — never an irreversible silent drop.
            self.store.update_memory(o["id"], status="superseded", superseded_by=mem["id"],
                                     reversible_until=until, conflicts_with=None)
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
            self._track_injection(m, now)
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

        def prune_eligible(m: dict[str, Any]) -> bool:
            below = self.effective_strength(m, now) < self.config.strength_floor
            if m.get("mtype") == "lesson":
                # Hand-authored / graduated lessons never prune; an un-graduated
                # auto-captured candidate may, once it has decayed below the floor.
                return below and self._is_candidate_lesson(m)
            # Decay-gated only: a genuinely-used memory is reinforced and stays
            # above the floor (so heavy in-session use is never mistaken for
            # noise). "Noise" — injected a lot but never proved useful — simply
            # isn't protected from this decay-prune (and the guardrail won't shield
            # it), so it ages out without insta-quarantining a strong memory.
            return below

        decay_ids = {m["id"] for m in actives if prune_eligible(m)}
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
            femb = self._embed(fused)
            self.store.update_memory(keep["id"], content=fused,
                                     embedding=femb, tokens=fused_tokens,
                                     embedder=self._embedder_tag(femb))
            self.store.update_memory(drop["id"], status="superseded", superseded_by=keep["id"],
                                     reversible_until=until)
            self.store.add_event(keep["id"], "merge",
                                 reason="compaction consolidated a near-duplicate",
                                 token_delta=-drop["tokens"])
            merged_ids.append(drop["id"])
            freed += drop["tokens"]

        for newer, older in plan.get("supersedes", []):
            self.store.update_memory(older["id"], status="superseded", superseded_by=newer["id"],
                                     reversible_until=until)
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
            targets = [r["memory"] for r in ranked
                       if r["sim"] >= self.config.forget_min_sim][:10]
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
                                 superseded_by=None, reversible_until=None,
                                 conflicts_with=None)
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
        """Hard-expire quarantined AND superseded memories past their reversible
        window (superseded rows get a window at supersede time), so old truth-
        replacements don't leak forever but stay restorable until then."""
        now = self.store.now() if now is None else now
        expired = [
            m
            for status in ("quarantined", "superseded")
            for m in self.store.list_memories(status=status)
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
            "health": self.health(),
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

    def conflicts(self) -> list[dict[str, Any]]:
        """Active same-subject memories flagged as *possible* contradictions the
        offline detector couldn't confidently auto-resolve — surfaced for review
        (both kept), the honest alternative to silently coexisting or burying."""
        out = []
        for m in self.store.list_memories(status="active"):
            cw = m.get("conflicts_with")
            if not cw:
                continue
            other = self.store.get_memory(cw)
            out.append({"memory": self._public_memory(m),
                        "conflicts_with": self._public_memory(other) if other else None})
        return out

    def superseded(self, limit: int = 50) -> list[dict[str, Any]]:
        """Recently superseded memories with what replaced them + why — the
        discoverable review surface so truth-replacement is never an invisible drop."""
        out = []
        for m in self.store.list_superseded(limit):
            by_id = m.get("superseded_by")
            by = self.store.get_memory(by_id) if by_id else None
            reason = next((e["reason"] for e in reversed(self.store.events_for(m["id"]))
                           if e["type"] == "supersede"), "")
            out.append({"memory": self._public_memory(m),
                        "superseded_by": self._public_memory(by) if by else None,
                        "reason": reason,
                        "reversible_until": m.get("reversible_until")})
        return out

    def session_context(self, query: Optional[str] = None,
                        token_budget: Optional[int] = None) -> dict[str, Any]:
        """What to inject at SessionStart: the most useful lessons, plus the most
        relevant memories for the query — the WHOLE payload packed under a budget.

        This is the hook-injection surface (how memory reaches the model without a
        tool call). Lessons no longer bypass the budget: they're ranked and packed
        under a lesson sub-budget (the rest goes to query-relevant memories), so a
        growing lesson corpus can't silently blow the context. Every injected
        memory is tracked (the push path feeds the usefulness loop), and lessons
        marked harmful are skipped."""
        now = self.store.now()
        budget = int(self.config.token_budget_default if token_budget is None else token_budget)
        budget = max(0, budget)

        # Rank lessons by usefulness (effective strength folds in harmful penalty,
        # candidate decay, and access); skip ones marked harmful; pack under the
        # lesson sub-budget. A '+N more' pointer keeps the omitted count honest.
        all_lessons = [m for m in self.store.list_memories(status="active")
                       if m.get("mtype") == "lesson"
                       and int(m.get("harmful_count", 0) or 0) == 0]
        all_lessons.sort(key=lambda m: (self.effective_strength(m, now), m["created_at"]),
                         reverse=True)
        lesson_budget = int(budget * self.config.lesson_budget_frac)
        packed_lessons: list[dict[str, Any]] = []
        for m in all_lessons:
            if self._mtok(packed_lessons + [m]) <= lesson_budget:
                packed_lessons.append(m)
        lessons_omitted = len(all_lessons) - len(packed_lessons)

        # The remainder of the FULL budget goes to query-relevant memories.
        injected = list(packed_lessons)
        memories: list[dict[str, Any]] = []
        if query:
            for r in self._rank(query, now):
                m = r["memory"]
                if r["sim"] <= 0 or m.get("mtype") == "lesson":
                    continue
                if self._mtok(injected + [m]) <= budget:
                    injected.append(m)
                    memories.append(m)

        # The push path is a real injection — feed the usefulness/recurrence loop
        # and the audit, so SessionStart isn't invisible to the flywheel.
        for m in injected:
            self._track_injection(m, now)
            self.store.add_event(m["id"], "recall_inject", reason="session_context (push)")

        return {
            "lessons": [self._public_memory(m) for m in packed_lessons],
            "memories": [self._public_memory(m) for m in memories],
            "lessons_omitted": lessons_omitted,
            "text": self._format_context(packed_lessons, memories, lessons_omitted),
            "tokens": self._mtok(injected),
        }

    def _format_context(self, lessons: list[dict[str, Any]],
                        memories: list[dict[str, Any]], lessons_omitted: int = 0) -> str:
        lines: list[str] = []
        if lessons:
            lines.append("Lessons learned (do not repeat these):")
            lines += [f"- {m['content']}" for m in lessons]
            if lessons_omitted:
                lines.append(f"- (+{lessons_omitted} more lessons — over the lesson budget; "
                             f"raise lesson_budget_frac or run `leptin compact`)")
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
            "inject_count": int(mem.get("inject_count", 0) or 0),
            "useful_count": int(mem.get("useful_count", 0) or 0),
            "harmful_count": int(mem.get("harmful_count", 0) or 0),
            "recur_sessions": int(mem.get("recur_sessions", 0) or 0),
            "conflicts_with": mem.get("conflicts_with"),
            "embedder": mem.get("embedder"),
        }

    # ------------------------------------------------------------- memory health
    def health(self) -> dict[str, Any]:
        """A 0–100 memory-health score + drift flags — the observable output of
        the loops. Storage/compression layers don't expose this."""
        now = self.store.now()
        actives = self.store.list_memories(status="active")
        n = len(actives)
        if n == 0:
            return {"score": 100, "active": 0, "stale_rate": 0.0, "noise_rate": 0.0,
                    "harmful": 0, "lessons": 0, "conflicts": 0, "auto_lessons": 0,
                    "embedder_drift": False, "drift": [], "grade": "A"}
        stale = sum(1 for m in actives if m.get("stale"))
        harmful = sum(1 for m in actives if int(m.get("harmful_count", 0) or 0) > 0)
        # Noise = injected a lot, never *explicitly* useful, AND decayed below the
        # floor (a strong, genuinely-used memory is never noise — that was the
        # false-positive). recur_sessions does NOT shield it.
        noise = sum(1 for m in actives
                    if m.get("mtype") != "lesson"
                    and int(m.get("inject_count", 0) or 0) >= self.config.noise_inject_count
                    and int(m.get("useful_count", 0) or 0) == 0
                    and self.effective_strength(m, now) < self.config.strength_floor)
        conflicts = sum(1 for m in actives if m.get("conflicts_with"))
        lessons = sum(1 for m in actives if m.get("mtype") == "lesson")
        auto_lessons = sum(1 for m in actives if self._is_candidate_lesson(m))
        embedder_drift = len({m.get("embedder") for m in actives if m.get("embedder")}) > 1
        stale_rate, noise_rate, harmful_rate = stale / n, noise / n, harmful / n
        # Normalized, floor-free, monotone score: weights divided by their sum so a
        # fully-degraded store maps to 0, a clean one to 100 (no pre-clamp underflow).
        w_stale, w_noise, w_harmful = 0.6, 0.3, 0.4
        wsum = w_stale + w_noise + w_harmful
        penalty = (w_stale * stale_rate + w_noise * noise_rate + w_harmful * harmful_rate) / wsum
        score = max(0, min(100, round(100 * (1 - penalty))))
        drift = []
        if stale_rate > self.config.drift_stale_rate:
            drift.append(f"{stale}/{n} memories are stale — run `leptin compact` or re-anchor sources")
        if noise_rate > self.config.drift_noise_rate:
            drift.append(f"{noise}/{n} memories are recalled-but-never-useful and cold — `leptin compact` will prune them")
        if conflicts:
            drift.append(f"{conflicts} possible contradiction(s) need review — run `leptin conflicts`")
        if embedder_drift:
            drift.append("mixed embedders in the store (a hosted→local fallback?) — run `leptin reembed`")
        grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D"
        return {"score": score, "grade": grade, "active": n, "stale_rate": round(stale_rate, 3),
                "noise_rate": round(noise_rate, 3), "harmful": harmful, "lessons": lessons,
                "conflicts": conflicts, "auto_lessons": auto_lessons,
                "embedder_drift": embedder_drift, "drift": drift}
