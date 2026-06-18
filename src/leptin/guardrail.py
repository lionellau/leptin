"""The recall guardrail — Leptin's safety net.

Before any destructive compaction commits, the guardrail re-runs a probe set
(``question -> expected_fact``) against the *post-diet* store inside an open
transaction. If recall would drop past ``guardrail_max_drop``, the whole
compaction is rolled back. Nothing is ever silently forgotten.

Probes come from two sources, combined at measure time:
  * user-supplied probes persisted in the ``probes`` table (always honoured)
  * auto-derived probes from the current high-strength active memories

This module deliberately does NOT import :mod:`leptin.engine` (avoiding a
circular import); it operates on an injected engine instance.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from leptin.engine import DietEngine

_WORD = re.compile(r"[a-z0-9']+")
_COVERAGE_THRESHOLD = 0.6


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def covers(retrieved_content: str, expected_fact: str) -> bool:
    """Is ``expected_fact`` substantively present in a retrieved memory?

    Used as a *fallback* for user probes that couldn't be linked to a specific
    memory id. Requires (a) most of the expected tokens to be present AND (b) a
    high overlap coefficient — both, so a short fact like "90 days" is not
    "covered" by an unrelated memory that merely happens to share one token.
    Identity-based coverage (see :meth:`Guardrail.measure`) is preferred and
    immune to this class of false positive entirely.
    """
    ew = _words(expected_fact)
    rw = _words(retrieved_content)
    if not ew:
        return True
    if not rw:
        return False
    inter = len(ew & rw)
    overlap = inter / min(len(ew), len(rw))
    containment = inter / len(ew)  # how much of the expected fact is present
    return overlap >= _COVERAGE_THRESHOLD and containment >= 0.8


# Backwards-compatible alias.
_covers = covers


class Guardrail:
    def __init__(self, engine: "DietEngine"):
        self.engine = engine

    # ------------------------------------------------------------- probe set
    def derive_probes(self, now: float) -> list[dict[str, Any]]:
        """Auto-probes guard *important* (above-floor) memories.

        Decay-eligible memories (below ``strength_floor``) are intentionally NOT
        auto-probed — they are exactly what compaction is allowed to prune. A
        user who cares about a specific weak memory adds an explicit probe, which
        is always honoured (see :meth:`build_probe_set`).
        """
        cfg = self.engine.config
        actives = self.engine.store.list_memories(status="active")
        keep = [
            (self.engine.effective_strength(m, now), m)
            for m in actives
            if self.engine.effective_strength(m, now) >= cfg.strength_floor
        ]
        keep.sort(key=lambda x: x[0], reverse=True)
        return [
            {"question": (m.get("subject") or "") + " " + m["content"],
             "expected_fact": m["content"], "source_memory_id": m["id"]}
            for _s, m in keep[: cfg.max_probes]
        ]

    def build_probe_set(self, now: float) -> list[dict[str, Any]]:
        cfg = self.engine.config
        user = [
            {"question": p["question"], "expected_fact": p["expected_fact"],
             "source_memory_id": p.get("source_memory_id")}
            for p in self.engine.store.list_probes()
        ]
        derived = self.derive_probes(now)
        combined: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for p in user + derived:  # user probes take precedence
            key = (p["question"].strip().lower(), p["expected_fact"].strip().lower())
            if key in seen:
                continue
            seen.add(key)
            combined.append(p)
        return combined[: max(cfg.max_probes, len(user))]

    # -------------------------------------------------------------- measure
    def measure(self, probes: list[dict[str, Any]], now: Optional[float] = None) -> float:
        """Fraction of probes whose expected fact is still retrievable.

        Measured against exactly what ``recall`` would inject (``_recall_preview``,
        budget + relevance gate), never a looser top-k — so the guardrail can't
        PASS while the real recall path silently drops a fact.

        Coverage is checked by *identity* when the probe is linked to a source
        memory (auto-probes always are; user probes are linked at ``add_probe``
        time): a probe counts as covered only if its source memory — or the live
        memory that supersedes/merged it — is actually injected. That closes the
        false-pass where an unrelated survivor shares a token with the fact.
        """
        if not probes:
            return 1.0
        now = self.engine.store.now() if now is None else now
        hits = 0
        for p in probes:
            injected = self.engine._recall_preview(p["question"], now)
            inj_ids = {m["id"] for m in injected}
            src = p.get("source_memory_id")
            if src:
                live = self.engine._live_id(src)
                covered = live is not None and live in inj_ids
            else:
                covered = any(covers(m["content"], p["expected_fact"]) for m in injected)
            if covered:
                hits += 1
        return hits / len(probes)

    # ------------------------------------------------------- guarded compact
    def guarded_compact(self, dry_run: bool = False) -> dict[str, Any]:
        engine = self.engine
        store = engine.store
        cfg = engine.config
        now = store.now()

        # Expire anything past its retention window first (purely additive, not
        # part of the guarded prune — these are already inactive).
        purged = 0 if dry_run else engine.purge_expired(now)

        probes = self.build_probe_set(now)
        recall_before = self.measure(probes, now)
        plan = engine.plan_compaction(now)
        projected_freed = sum(m["tokens"] for m in plan["decayed"])

        if not plan["decayed"]:
            store.add_probe_run("compact", recall_before, recall_before, True, False)
            return self._report(
                merged=0, superseded=0, decayed=0, projected=0,
                recall_before=recall_before, recall_after=recall_before,
                passed=True, rolled_back=False, dry_run=dry_run, diff=[], purged=purged,
            )

        store.begin()
        committed = False
        recall_after = recall_before
        try:
            applied = engine.apply_compaction(plan, now)
            recall_after = self.measure(probes, now)  # sees pending changes
            passed = recall_after >= recall_before - cfg.guardrail_max_drop
            if dry_run or not passed:
                store.rollback()
            else:
                store.commit()
                committed = True
        except Exception:
            store.rollback()
            # Preserve the trust audit trail even on an aborted compaction.
            store.add_probe_run("compact", recall_before, recall_after, False, True)
            raise

        rolled_back = (not committed) and (not dry_run)
        store.add_probe_run("compact", recall_before, recall_after, passed,
                            rolled_back)

        tokens_saved = projected_freed if committed else 0
        if committed:
            engine._log_footprint("compact", reduced=projected_freed,
                                  detail={"decayed": len(applied["decayed"])})

        diff = [{"memory_id": mid, "action": "decayed"} for mid in plan_ids(plan)]
        return self._report(
            merged=len(applied["merged"]) if committed else 0,
            superseded=len(applied["superseded"]) if committed else 0,
            decayed=len(plan["decayed"]),
            projected=projected_freed,
            recall_before=recall_before, recall_after=recall_after,
            passed=passed, rolled_back=rolled_back, dry_run=dry_run, diff=diff,
            tokens_saved=tokens_saved, purged=purged,
        )

    def _report(self, *, merged, superseded, decayed, projected, recall_before,
                recall_after, passed, rolled_back, dry_run, diff, tokens_saved=0,
                purged=0):
        return {
            "merged": merged,
            "superseded": superseded,
            "decayed": decayed,
            "purged": purged,
            "projected_tokens_saved": projected,
            "tokens_saved": tokens_saved,
            "dry_run": dry_run,
            "guardrail": {
                "recall_before": round(recall_before, 4),
                "recall_after": round(recall_after, 4),
                "passed": passed,
                "rolled_back": rolled_back,
                "max_drop": self.engine.config.guardrail_max_drop,
            },
            "diff": diff,
        }


def plan_ids(plan: dict[str, Any]) -> list[str]:
    return [m["id"] for m in plan.get("decayed", [])]
