"""Self-tuning — Leptin's outer control loop (PRD §13).

Leptin already *measures* itself (savings ledger + recall guardrail). The tuner
closes the loop: it replays the user's own data under candidate configs, and
commits a policy change only when it's a net win on a **held-out** probe set —
else it leaves the config untouched. Same trust DNA as the guardrail, applied to
the policy itself.

Design constraints (all enforced here):
- **Deterministic & offline.** No randomness, no LLM calls on the default path.
  All evaluation is read-only replay over SQLite.
- **Cheap.** Read-only candidate evals on a bounded query sample; cadence-
  triggered, never per-op.
- **Safe.** Held-out gate + dual-metric (recall AND savings) accept; locked knobs
  can never be touched; every accepted change is a reversible evolution-ledger row;
  a meta-guardrail freezes tuning after repeated failures.

Like :mod:`leptin.guardrail`, this module never imports :mod:`leptin.engine` at
load time (it constructs candidate engines lazily) to avoid a circular import.
"""

from __future__ import annotations

import dataclasses
import hashlib
from typing import TYPE_CHECKING, Any, Optional

from leptin.config import Config

if TYPE_CHECKING:  # pragma: no cover
    from leptin.engine import DietEngine

# The continuous knobs the tuner may evolve, with hard clamps. These are the
# knobs whose effect is observable by *read-only* recall replay on the existing
# store. (dedup_threshold is a write-time knob — tuning it needs re-ingestion,
# deferred to v0.3.)
ACTION_SPACE: dict[str, tuple[float, float]] = {
    "decay_half_life_days": (7.0, 45.0),
    "strength_floor": (0.05, 0.30),
    "recall_rel_floor": (0.40, 0.75),
    "token_budget_default": (500.0, 3000.0),
    "access_boost": (0.20, 0.60),
}

STEP_GRID: dict[str, tuple[float, ...]] = {
    "decay_half_life_days": (0.8, 1.0, 1.25),
    "strength_floor": (0.8, 1.0, 1.25),
    "recall_rel_floor": (0.9, 1.0, 1.1),
    "token_budget_default": (0.8, 1.0, 1.2),
    "access_boost": (0.8, 1.0, 1.2),
}

# Never proposed by the tuner (anti-Goodhart / anti-misevolution). Includes the
# v1.3 safety/correctness knobs: the noise, lesson, harmful, drift, and detector
# constants must stay where a human (not a self-optimiser) put them.
LOCKED_KNOBS = frozenset({
    "guardrail_max_drop", "max_probes", "reversible_window_days", "recall_k",
    "naive_top_k", "embedding_model", "llm_model", "price_model", "price_table",
    "embedding_dim", "backend", "contradiction_threshold", "dedup_threshold",
    "recall_min_sim",
    "stale_penalty", "harmful_penalty", "forget_min_sim", "noise_inject_count",
    "recur_cooldown_seconds", "harmful_stale_threshold", "drift_stale_rate",
    "drift_noise_rate", "lesson_budget_frac", "max_auto_lessons",
    "candidate_lesson_half_life_days", "procedural_halflife_mult",
    "task_halflife_mult", "rank_candidate_limit",
})

OBJECTIVE_W = {"balanced": 0.5, "savings": 0.8, "recall": 0.2}
_UCB_ALPHA = 0.7
_MAX_REJECTS = 3


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def validate_proposal(knob: str) -> None:
    """Guard: the tuner may never touch a locked knob (esp. the guardrail)."""
    if knob in LOCKED_KNOBS or knob not in ACTION_SPACE:
        raise ValueError(f"tuner attempted to modify locked/unknown knob: {knob}")


class Evaluator:
    """Deterministic, read-only evaluation of a candidate Config on the live store."""

    def __init__(self, engine: "DietEngine"):
        self.engine = engine

    def query_log(self, now: float) -> list[str]:
        cfg = self.engine.config
        seen: set[str] = set()
        out: list[str] = []
        for r in reversed(self.engine.store.ledger_rows()):
            if r["operation"] != "recall":
                continue
            q = (r.get("detail") or {}).get("query")
            if q and q not in seen:
                seen.add(q)
                out.append(q)
            if len(out) >= cfg.tune_replay_n:
                break
        return out

    def _candidate_engine(self, cfg: Config) -> "DietEngine":
        from leptin.engine import DietEngine

        return DietEngine(self.engine.store, cfg, embedder=self.engine.embedder,
                          merger=self.engine.merger, session_id=self.engine.session_id)

    def evaluate(self, cfg: Config, probes: list[dict[str, Any]],
                 query_log: list[str], now: float) -> dict[str, float]:
        cand = self._candidate_engine(cfg)
        recall = cand.guardrail.measure(probes, now)
        reductions = []
        for q in query_log:
            m = cand._recall_eval(q, now)
            if m["baseline_tokens"] > 0:
                reductions.append((m["baseline_tokens"] - m["actual_tokens"]) / m["baseline_tokens"])
        # Signed (NOT clamped at 0): a config that over-injects vs. naive scores
        # negative, giving the optimizer a gradient toward fixing it.
        reduction = sum(reductions) / len(reductions) if reductions else 0.0
        return {"recall": recall, "reduction": reduction}


class Tuner:
    def __init__(self, engine: "DietEngine"):
        self.engine = engine
        self.evaluator = Evaluator(engine)

    # --- persisted tune state (kept in the config table; filtered out of Config) ---
    def _state(self) -> dict[str, Any]:
        return self.engine.store.load_config()

    def _set(self, **kv: Any) -> None:
        self.engine.store.save_config(kv)

    def _objective(self, e: dict[str, float]) -> float:
        w = OBJECTIVE_W.get(self.engine.config.tune_objective, 0.5)
        return w * e["reduction"] + (1.0 - w) * e["recall"]

    # ------------------------------------------------------------ triggering
    def should_tune(self, now: float) -> tuple[bool, Optional[str]]:
        cfg = self.engine.config
        if not cfg.self_tune_enabled:
            return False, None
        st = self._state()
        frozen = st.get("_tune.frozen_until")
        if isinstance(frozen, (int, float)) and now < frozen:
            return False, "frozen"
        last_at = st.get("_tune.last_at", 0.0) or 0.0
        # Count-delta marker (clock-independent: robust under a frozen test clock).
        last_count = int(st.get("_tune.last_count", 0) or 0)
        total = self.engine.store.count_memories(None)
        if total - last_count >= cfg.tune_min_new_memories:
            return True, "new_memories"
        if last_at and now - last_at >= cfg.tune_max_interval_days * 86400.0:
            return True, "cadence"
        return False, None

    # ------------------------------------------------------------- the cycle
    @staticmethod
    def _split_probes(probes: list[dict[str, Any]]):
        visible, held = [], []
        for p in probes:
            # blake2b, not builtin hash(): the latter is salted per-process
            # (PYTHONHASHSEED), which silently broke the module's 'deterministic'
            # contract and could vary/empty the held set run-to-run.
            digest = hashlib.blake2b(p["question"].encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest, "big") % 5
            (held if bucket == 0 else visible).append(p)
        # Small stores: fall back to gating on whatever we have.
        if not held:
            held = visible
        if not visible:
            visible = held
        return visible, held

    def _ucb_select(self, ucb: dict[str, list[float]], t: int) -> str:
        import math

        best_knob, best_score = None, float("-inf")
        for knob in ACTION_SPACE:
            q, n = ucb.get(knob, [0.0, 0.0])
            if n <= 0:
                return knob  # try every coordinate at least once (deterministic order)
            score = q + _UCB_ALPHA * math.sqrt(math.log(max(1, t)) / n)
            if score > best_score:
                best_score, best_knob = score, knob
        return best_knob or next(iter(ACTION_SPACE))

    def tune(self, dry_run: bool = False, trigger: str = "manual") -> dict[str, Any]:
        engine = self.engine
        cfg = engine.config
        now = engine.store.now()
        engine._settle_embedder()

        st = self._state()
        frozen = st.get("_tune.frozen_until")
        # The meta-guardrail freezes only the AUTOMATIC loop; an explicit manual
        # tune is always allowed (and clears the freeze).
        if trigger == "auto" and isinstance(frozen, (int, float)) and now < frozen:
            return {"status": "frozen", "frozen_until": frozen, "accepted": False,
                    "changes": [], "trigger": trigger,
                    "objective_before": 0.0, "objective_after": 0.0,
                    "recall_before": 0.0, "recall_after": 0.0,
                    "reduction_before": 0.0, "reduction_after": 0.0,
                    "llm_calls": 0, "tune_tokens": 0}

        probes = engine.guardrail.build_probe_set(now)
        visible, held = self._split_probes(probes)
        qlog = self.evaluator.query_log(now)

        base_vis = self.evaluator.evaluate(cfg, visible, qlog, now)
        base_held = self.evaluator.evaluate(cfg, held, qlog, now)
        base_R = self._objective(base_vis)

        ucb = {k: list(v) for k, v in (st.get("_tune.ucb") or {}).items()}
        t = int(st.get("_tune.t", 0) or 0)

        working = cfg
        best_cfg, best_eval, best_R = cfg, base_vis, base_R
        changes: list[dict[str, Any]] = []

        for _ in range(cfg.tune_max_coords_per_cycle):
            knob = self._ucb_select(ucb, t)
            validate_proposal(knob)
            lo, hi = ACTION_SPACE[knob]
            cur_val = float(getattr(working, knob))
            cur_R = self._objective(self.evaluator.evaluate(working, visible, qlog, now))

            best_local_val, best_local_R = None, cur_R
            tried: set[float] = set()
            for f in STEP_GRID[knob]:
                v = round(_clamp(cur_val * f, lo, hi), 6)
                if v in tried:
                    continue
                tried.add(v)
                cand_cfg = dataclasses.replace(working, **{knob: v})
                e = self.evaluator.evaluate(cand_cfg, visible, qlog, now)
                if e["recall"] < base_vis["recall"] - cfg.guardrail_max_drop:
                    continue  # infeasible
                r = self._objective(e)
                if r > best_local_R + 1e-9:
                    best_local_R, best_local_val = r, v

            reward = 0.0
            if best_local_val is not None:
                new_val = round(_clamp(0.7 * cur_val + 0.3 * best_local_val, lo, hi), 6)
                cand_cfg = dataclasses.replace(working, **{knob: new_val})
                e = self.evaluator.evaluate(cand_cfg, visible, qlog, now)
                new_R = self._objective(e)
                reward = new_R - cur_R
                if new_R > cur_R + 1e-12 and new_val != cur_val:
                    changes.append({"knob": knob, "old": cur_val, "new": new_val,
                                    "direction": "up" if new_val > cur_val else "down"})
                    working = cand_cfg
                    if new_R > best_R:
                        best_R, best_cfg, best_eval = new_R, cand_cfg, e

            q, n = ucb.get(knob, [0.0, 0.0])
            n += 1
            q += (reward - q) / n  # incremental mean
            ucb[knob] = [q, n]
            t += 1

        # Held-out, dual-metric accept gate.
        cand_held = self.evaluator.evaluate(best_cfg, held, qlog, now)
        accept = (
            bool(changes)
            and cand_held["recall"] >= base_held["recall"] - cfg.guardrail_max_drop
            and cand_held["reduction"] >= base_held["reduction"] - cfg.tune_savings_floor
            and best_R > base_R + cfg.tune_epsilon
        )

        result = {
            "trigger": trigger, "dry_run": dry_run, "accepted": accept,
            "changes": changes,
            "objective_before": round(base_R, 6), "objective_after": round(best_R, 6),
            "recall_before": round(base_held["recall"], 4),
            "recall_after": round(cand_held["recall"], 4),
            "reduction_before": round(base_held["reduction"], 4),
            "reduction_after": round(cand_held["reduction"], 4),
            "llm_calls": 0, "tune_tokens": 0,
        }

        if dry_run:
            return result

        # Persist UCB state + cycle bookkeeping.
        cycle = int(st.get("_tune.cycle", 0) or 0) + 1
        self._set(**{"_tune.ucb": ucb, "_tune.t": t, "_tune.cycle": cycle,
                     "_tune.last_at": now,
                     "_tune.last_count": self.engine.store.count_memories(None)})

        if accept:
            self._commit(best_cfg, changes, cand_held, base_held, cfg, trigger, cycle, now)
            self._set(**{"_tune.rejects": 0, "_tune.frozen_until": 0})
        elif trigger == "auto":
            # Meta-guardrail applies only to the automatic loop.
            rejects = int(st.get("_tune.rejects", 0) or 0) + 1
            self._set(**{"_tune.rejects": rejects})
            if rejects >= _MAX_REJECTS:
                # The action space can't fix the current data — stop auto-tuning.
                engine.config.self_tune_enabled = False
                self._set(**{"_tune.frozen_until": now + cfg.tune_freeze_days * 86400.0,
                             "self_tune_enabled": False})
                result["frozen"] = True

        self.engine.store.add_tune_run(
            trigger=trigger, cycle=cycle,
            recall_before=base_held["recall"], recall_after=cand_held["recall"],
            reduction_before=base_held["reduction"], reduction_after=cand_held["reduction"],
            accepted=accept, rolled_back=False, llm_calls=0, tune_tokens=0,
        )
        return result

    def _commit(self, new_cfg, changes, cand_held, base_held, old_cfg, trigger, cycle, now):
        store = self.engine.store
        latest = store.latest_config_version(accepted_only=True)
        if latest is None:
            # Seed a baseline snapshot so rollback always has a prior state.
            base_id = store.add_config_version(
                knob=None, old_value=None, new_value=None, direction="baseline",
                accepted=True, reason="pre-tune baseline", config_json=old_cfg.to_dict(),
                parent_id=None)
        else:
            base_id = latest["id"]
        store.add_config_version(
            knob=",".join(c["knob"] for c in changes),
            old_value={c["knob"]: c["old"] for c in changes},
            new_value={c["knob"]: c["new"] for c in changes},
            direction="tuned", accepted=True,
            reason=f"net win (trigger={trigger}, cycle={cycle})",
            config_json=new_cfg.to_dict(), parent_id=base_id)
        store.save_config(new_cfg.to_dict())
        self.engine.config = new_cfg

    # --------------------------------------------------------------- rollback
    def rollback(self, version: Optional[int] = None) -> dict[str, Any]:
        store = self.engine.store
        if version is not None:
            target = store.get_config_version(version)
            if not target:
                return {"rolled_back": False, "reason": f"no version {version}"}
        else:
            latest = store.latest_config_version(accepted_only=True)
            if not latest or latest.get("direction") == "baseline":
                return {"rolled_back": False, "reason": "no tuned version to undo"}
            parent_id = latest.get("parent_id")
            target = store.get_config_version(parent_id) if parent_id else None
            if not target:
                return {"rolled_back": False, "reason": "no prior version"}

        restored = Config.from_dict(target["config_json"])
        store.save_config(restored.to_dict())
        self.engine.config = restored
        store.add_config_version(
            knob=None, old_value=None, new_value=None, direction="rollback",
            accepted=True, reason=f"rollback to version {target['id']}",
            config_json=restored.to_dict(), parent_id=target["id"])
        return {"rolled_back": True, "version": target["id"],
                "config": restored.to_dict()}

    # ----------------------------------------------------------------- report
    def report(self) -> Optional[dict[str, Any]]:
        store = self.engine.store
        st = self._state()
        if store.count_tune_runs() == 0 and not st.get("_tune.last_at"):
            return None
        runs = store.list_tune_runs(200)
        accepted = sum(1 for r in runs if r.get("accepted"))
        last = runs[0] if runs else None
        latest_v = store.latest_config_version(accepted_only=True)
        return {
            "enabled": self.engine.config.self_tune_enabled,
            "last_tune_at": st.get("_tune.last_at"),
            "cycles": int(st.get("_tune.cycle", 0) or 0),
            "accepted": accepted,
            "rejected": len(runs) - accepted,
            "last_delta": (round((last["reduction_after"] or 0) - (last["reduction_before"] or 0), 4)
                           if last else None),
            "current_version": latest_v["id"] if latest_v else None,
            "frozen_until": st.get("_tune.frozen_until"),
            "llm_calls": sum(int(r.get("llm_calls") or 0) for r in runs),
            "tune_tokens": sum(int(r.get("tune_tokens") or 0) for r in runs),
        }

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.engine.store.list_config_versions(limit)
