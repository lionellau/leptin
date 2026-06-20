"""``leptin`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from typing import Optional

from leptin import __version__

DEFAULT_DB = os.path.expanduser("~/.leptin/memory.db")


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _leptin_command() -> str:
    """Resolve a command Claude Code can actually launch.

    Prefer `leptin` if it's on PATH; otherwise fall back to the absolute path of
    the installed console script (works for venv / non-PATH installs)."""
    on_path = shutil.which("leptin")
    if on_path:
        return "leptin"
    candidate = os.path.join(os.path.dirname(sys.executable), "leptin")
    return candidate if os.path.exists(candidate) else "leptin"


def _mcp_block(db_path: str) -> str:
    block = {
        "mcpServers": {
            "leptin": {
                "command": _leptin_command(),
                "args": ["serve", "--db", db_path],
            }
        }
    }
    return json.dumps(block, indent=2)


# --- host wiring (so an agent can install Leptin on itself) -------------------
_HOOK_EVENTS = (
    ("SessionStart", "session-start"),
    ("UserPromptSubmit", "user-prompt-submit"),
    ("PostToolUse", "post-tool-use"),
    ("Stop", "stop"),
    ("PreCompact", "pre-compact"),
)
_MINIMAL_HOOK_EVENTS = (
    ("SessionStart", "session-start"),
    ("Stop", "stop"),
)


def _host_settings_path(host: str) -> Optional[str]:
    host = (host or "claude-code").lower()
    if "claude" in host:
        return os.path.expanduser("~/.claude/settings.json")
    if "codex" in host:
        return os.path.expanduser("~/.codex/settings.json")  # best-effort
    return None


def _connect_block(db: str, minimal: bool = False) -> dict:
    leptin = _leptin_command()
    events = _MINIMAL_HOOK_EVENTS if minimal else _HOOK_EVENTS
    hooks = {
        evt: [{"hooks": [{"type": "command", "command": f"{leptin} hook {hk} --db {db}"}]}]
        for evt, hk in events
    }
    return {"mcpServers": {"leptin": {"command": leptin, "args": ["serve", "--db", db]}},
            "hooks": hooks}


def _merge_host_config(existing: dict, block: dict) -> tuple[dict, int]:
    """Idempotent deep-merge of Leptin's mcpServers + hooks into existing host
    settings, WITHOUT clobbering anything else. Hook entries are keyed by command
    string, so re-running is a safe no-op. Returns (merged, n_changes)."""
    merged = dict(existing)
    changes = 0
    servers = dict(merged.get("mcpServers") or {})
    if servers.get("leptin") != block["mcpServers"]["leptin"]:
        servers["leptin"] = block["mcpServers"]["leptin"]
        changes += 1
    merged["mcpServers"] = servers
    hooks = dict(merged.get("hooks") or {})
    for evt, entries in block["hooks"].items():
        our_cmd = entries[0]["hooks"][0]["command"]
        cur = list(hooks.get(evt) or [])
        present = any(
            any(h.get("command") == our_cmd for h in (e.get("hooks") or []))
            for e in cur if isinstance(e, dict)
        )
        if not present:
            cur.append(entries[0])
            changes += 1
        hooks[evt] = cur
    merged["hooks"] = hooks
    return merged, changes


def _write_host_config(host: str, block: dict, dry_run: bool = False) -> dict:
    """Write Leptin's wiring into the host's settings.json: back up first, merge
    idempotently, refuse to touch a malformed file. The high-trust mutation that
    lets an agent install Leptin on itself — made safe."""
    path = _host_settings_path(host)
    if not path:
        return {"ok": False, "reason": f"unknown host '{host}' (try claude-code or codex)", "path": None}
    existing: dict = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                existing = json.loads(f.read() or "{}")
        except (json.JSONDecodeError, ValueError):
            return {"ok": False, "path": path,
                    "reason": f"{path} is not valid JSON — refusing to overwrite; back it up and retry"}
        if not isinstance(existing, dict):
            return {"ok": False, "path": path, "reason": f"{path} is not a JSON object"}
    merged, changes = _merge_host_config(existing, block)
    if dry_run:
        return {"ok": True, "dry_run": True, "path": path, "changes": changes, "preview": merged}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = f"{path}.leptin-bak-{int(time.time())}"
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(merged, indent=2) + "\n")
    return {"ok": True, "path": path, "changes": changes, "backup": backup}


def _host_wiring_status(host: str) -> dict:
    """Post-install self-check: is Leptin actually wired into the host config?
    Returns {level: ok|warn|error, detail}. Machine-readable via `leptin doctor --json`."""
    host = (host or "claude-code").lower()
    path = _host_settings_path(host)
    if not path or not os.path.exists(path):
        return {"level": "warn", "detail": f"not wired into {host} — run `leptin setup {host}`"}
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.loads(f.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        return {"level": "error", "detail": f"{path} is not valid JSON"}
    server = (cfg.get("mcpServers") or {}).get("leptin")
    if not server:
        return {"level": "warn", "detail": f"leptin MCP server not in {path} — run `leptin setup {host}`"}
    cmd = server.get("command")
    resolves = bool(cmd and (shutil.which(cmd) or os.path.exists(cmd)))
    n_hooks = sum(
        1 for evt, entries in (cfg.get("hooks") or {}).items()
        if any(any("leptin" in (h.get("command", "") or "") for h in (e.get("hooks") or []))
               for e in (entries or []) if isinstance(e, dict))
    )
    if not resolves:
        return {"level": "warn",
                "detail": f"leptin MCP present but command '{cmd}' doesn't resolve on PATH — `pip install leptin-hlp`"}
    return {"level": "ok", "detail": f"MCP + {n_hooks} hook event(s) wired in {path}"}


def cmd_serve(args) -> int:
    from leptin.server import serve

    serve(args.db)
    return 0


def cmd_bench(args) -> int:
    from leptin import bench

    if getattr(args, "eval_contradiction", False):
        path = args.eval_contradiction if isinstance(args.eval_contradiction, str) else None
        res = bench.eval_contradiction(path)
        _print_json(res)
        return 0 if res["f1"] >= 0.7 else 1
    r = bench.main(budget=args.budget, naive_top_k=args.top_k, dataset=args.dataset,
                   limit=args.limit, embedding_model=args.embedding_model,
                   llm_model=args.llm_model)
    if args.json:
        _print_json(r)
    return 0 if r["headline_pass"] else 1


def cmd_init(args) -> int:
    from leptin.api import Leptin

    mem = Leptin(args.db)
    mem.save_config()
    mem.close()
    print(f"Initialized Leptin store at: {mem.store.path}")
    print("\nAdd this to your Claude Code / Codex MCP config:\n")
    print(_mcp_block(args.db))
    print("\nThen restart the client and ask the agent to remember something.")
    print("\n# Tip: one command wires hooks + MCP and verifies it (an agent can run this itself):")
    print("#   leptin setup claude-code && leptin doctor --json")
    return 0


def cmd_report(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.diet_report(args.window))
    return 0


def cmd_remember(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.remember(args.content, subject=args.subject, source=args.source,
                                 mtype=args.type, source_ref=args.source_ref))
    return 0


def cmd_lesson(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.remember_lesson(args.content, subject=args.subject))
    return 0


def cmd_stale(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.flag_stale(args.source_ref))
    return 0


def cmd_feedback(args) -> int:
    from leptin.api import Leptin

    signal = "harmful" if args.harmful else "useful"
    with Leptin(args.db) as mem:
        _print_json(mem.record_feedback(args.memory_id, signal))
    return 0


def cmd_health(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.health())
    return 0


def cmd_conflicts(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.conflicts())
    return 0


def cmd_superseded(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.superseded(limit=args.limit))
    return 0


def cmd_reembed(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.reembed())
    return 0


def cmd_demo(args) -> int:
    """The 60-second 'see it': a reversed decision, naive store vs Leptin.

    Runs entirely in-memory (touches nothing on disk) so anyone can watch the
    one thing Leptin does that a store/compressor doesn't — keep the *current*
    decision authoritative after you change your mind."""
    from leptin.api import Leptin
    from leptin.config import Config

    q = "what package manager do we use"
    naive = Leptin(":memory:", config=Config(dedup_threshold=2.0, contradiction_threshold=2.0))
    lep = Leptin(":memory:")
    for m in (naive, lep):
        m.remember("We use pnpm as our package manager.", subject="pkg")

    def recall(m):
        return [x["content"] for x in m.recall(q)["memories"]]

    print("\n  Leptin demo — what happens when a decision gets reversed")
    print("  " + "-" * 60)
    print("  ① The team is on pnpm. The agent recalls it correctly:")
    print(f"       recall(\"{q}\")  →  {recall(lep)}")
    print("\n  ② You switch to bun. The agent stores the new decision:")
    r = lep.remember("We use bun as our package manager.", subject="pkg")
    naive.remember("We use bun as our package manager.", subject="pkg")
    print(f"       remember(\"We use bun…\")  →  action={r['action']}"
          f"  (old fact kept, reversible: `leptin superseded`)")
    print("\n  ③ Next session, the agent asks again:")
    print(f"       a naive store →  {recall(naive)}")
    print("                         ↑ still serving the abandoned 'pnpm' — the agent acts on it")
    print(f"       Leptin        →  {recall(lep)}")
    print("                         ↑ only the current truth")
    print("\n  That's the wedge: not storing or shrinking memory — keeping it CORRECT.")
    print("  Reproduce the measured version with:  leptin bench\n")
    naive.close()
    lep.close()
    return 0


def cmd_recall(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.recall(args.query, token_budget=args.budget, k=args.k))
    return 0


def cmd_compact(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.compact(dry_run=args.dry_run))
    return 0


def cmd_inspect(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        _print_json(mem.inspect(memory_id=args.memory_id, query=args.query))
    return 0


def cmd_dashboard(args) -> int:
    from leptin.dashboard import serve_dashboard

    serve_dashboard(args.db, host=args.host, port=args.port)
    return 0


def cmd_hook(args) -> int:
    """Lifecycle-hook entrypoint for Claude Code / Codex (same field names).

    SessionStart / UserPromptSubmit → emit lessons + relevant memory as
    `additionalContext` (memory reaches the model with no tool call).
    Stop / SessionEnd / PreCompact → run guardrailed compaction in the background.
    Reads the host's hook JSON on stdin; never throws (a hook must not break the session).
    """
    from leptin.api import Leptin

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, ValueError):
        payload = {}
    event = (args.event or payload.get("hook_event_name") or "").lower().replace("_", "-")
    try:
        # Derive the session id from the host payload so SessionStart and
        # UserPromptSubmit in the same host session share one id (no double-count
        # of cross-session recurrence in the usefulness loop).
        with Leptin(args.db, session_id=payload.get("session_id")) as mem:
            if event in ("session-start", "sessionstart", "user-prompt-submit", "userpromptsubmit"):
                query = payload.get("prompt") or payload.get("user_prompt")
                ctx = mem.session_context(query=query)
                text = ctx["text"]
                if text:
                    hook_event = "UserPromptSubmit" if "prompt" in event else "SessionStart"
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": hook_event, "additionalContext": text}}))
            elif event in ("post-tool-use", "posttooluse"):
                # Mistake→lesson loop: a *genuinely failed* tool call becomes a
                # decaying candidate lesson that graduates to permanent only if it
                # recurs (so the corpus stays bounded — no minting from benign output).
                lesson = _lesson_from_failure(payload)
                if lesson:
                    mem.capture_lesson(lesson)
            elif event in ("stop", "session-end", "sessionend", "pre-compact", "precompact"):
                mem.compact()  # decay + guardrailed prune; keeps the store clean
    except Exception:  # noqa: BLE001 — a hook must never break the host session
        pass
    return 0


def _lesson_from_failure(payload: dict) -> Optional[str]:
    """Turn a *genuinely failed* PostToolUse payload into an anti-pattern line.

    Gated on real failure signals (is_error / non-zero return / non-empty stderr /
    an explicit error field) — NOT on the substring "error"/"fail" appearing in
    otherwise-benign output, which used to mint permanent lessons from nothing."""
    resp = payload.get("tool_response") or payload.get("tool_result") or {}
    rd = resp if isinstance(resp, dict) else {}
    rc = rd.get("returncode", rd.get("exit_code"))
    is_error = bool(
        payload.get("is_error") or rd.get("is_error") or payload.get("error")
        or (isinstance(rc, int) and rc != 0)
        or (isinstance(rd.get("stderr"), str) and rd.get("stderr").strip())
    )
    if not is_error:
        return None
    text = (rd.get("error") or rd.get("stderr")
            or (resp if isinstance(resp, str) else "") or str(payload.get("error") or ""))
    tool = payload.get("tool_name") or payload.get("tool") or "a tool"
    cmd = ""
    ti = payload.get("tool_input") or {}
    if isinstance(ti, dict):
        cmd = ti.get("command") or ti.get("file_path") or ""
    summary = (text or "").strip().splitlines()[0][:160] if text else "it failed"
    detail = f" ({cmd})" if cmd else ""
    return f"Avoid: {tool}{detail} failed — {summary}"


def cmd_connect(args) -> int:
    """Wire Leptin into a coding-agent host. ``--write`` edits the host config
    directly (backed up + idempotent); by default it prints the block to paste."""
    host = (args.host or "claude-code").lower()
    block = _connect_block(args.db, minimal=getattr(args, "minimal", False))

    if getattr(args, "write", False) or getattr(args, "dry_run", False):
        res = _write_host_config(host, block, dry_run=getattr(args, "dry_run", False))
        if not res["ok"]:
            print(f"✗ {res['reason']}", file=sys.stderr)
            return 1
        if res.get("dry_run"):
            print(f"[dry-run] would update {res['path']} ({res['changes']} change(s)):\n")
            print(json.dumps(res["preview"], indent=2))
            return 0
        note = "already wired (no changes)" if res["changes"] == 0 else f"{res['changes']} change(s)"
        print(f"✓ Wired Leptin into {host}: {res['path']}  ({note})")
        if res.get("backup"):
            print(f"  backup: {res['backup']}")
        print("  Restart the client to load it. Only `remember`/`recall` reach the model "
              "(LEPTIN_MCP_TOOLS=all for the rest).")
        return 0

    # Default: print for manual paste (safe — no host mutation).
    path = _host_settings_path(host) or "your host config"
    print(f"Add this to your {host} config ({path}):\n")
    print(json.dumps(block, indent=2))
    print(f"\nOr have Leptin write it for you:  leptin connect {host} --write")
    print(f"Or let your agent install itself:  leptin setup {host}")
    print("Only `remember`/`recall` are exposed to the model; expose all with LEPTIN_MCP_TOOLS=all.")
    return 0


def cmd_setup(args) -> int:
    """The one command an agent runs to install Leptin on itself: create the store,
    write the host config (backed up, idempotent), and verify the wiring. Prints a
    single PASS/FAIL line and exits non-zero on any failure (agent-checkable)."""
    from leptin.api import Leptin

    host = (args.host or "claude-code").lower()
    with Leptin(args.db) as mem:
        mem.save_config()
        store_path = mem.store.path

    block = _connect_block(args.db, minimal=getattr(args, "minimal", False))
    res = _write_host_config(host, block, dry_run=getattr(args, "dry_run", False))
    if not res["ok"]:
        print(f"✗ setup failed: {res['reason']}", file=sys.stderr)
        return 1
    if res.get("dry_run"):
        print(f"[dry-run] store at {store_path}; would wire {res['path']} ({res['changes']} change(s))")
        return 0

    status = _host_wiring_status(host)
    ok = status["level"] != "error"
    print(f"{'✓' if ok else '✗'} Leptin "
          f"{'installed and wired into' if ok else 'install incomplete for'} {host}")
    print(f"  store:  {store_path}")
    print(f"  config: {res['path']}" + (f"  (backup: {res['backup']})" if res.get("backup") else ""))
    print(f"  wiring: {status['detail']}")
    if ok:
        print("  Restart the client — your agent now has persistent, self-correcting memory.")
    return 0 if ok else 1


def cmd_doctor(args) -> int:
    import importlib.util
    import platform

    from leptin import __version__
    from leptin.api import Leptin
    from leptin.config import Config
    from leptin.storage import SCHEMA_VERSION

    checks: list[tuple[str, str, str]] = []  # (level, name, detail)

    def add(level, name, detail):
        checks.append((level, name, detail))

    # --- runtime ---
    pyv = sys.version_info
    add("ok" if pyv >= (3, 10) else "error", "Python",
        f"{platform.python_version()} ({'>=3.10' if pyv >= (3, 10) else 'need >=3.10'})")
    add("ok", "Leptin", __version__)

    # --- store ---
    db_exists = os.path.exists(args.db)
    try:
        mem = Leptin(args.db)
        uv = mem.store.conn.execute("PRAGMA user_version").fetchone()[0]
        counts = {s: mem.store.count_memories(s)
                  for s in ("active", "superseded", "quarantined", "deleted")}
        size = os.path.getsize(mem.store.path) if os.path.exists(mem.store.path) else 0
        add("ok" if uv == SCHEMA_VERSION else "warn", "Store",
            f"{mem.store.path} | schema v{uv}/{SCHEMA_VERSION} | "
            f"{counts['active']} active, {counts['superseded']} superseded, "
            f"{counts['quarantined']} quarantined | {size/1024:.1f} KiB"
            + ("" if db_exists else " (newly created)"))
        cfg = mem.config
    except Exception as exc:  # noqa: BLE001
        add("error", "Store", f"failed to open {args.db}: {exc}")
        mem, cfg = None, Config()

    # --- models / hosted readiness ---
    emb = cfg.embedding_model
    if emb in ("local-hash", "heuristic", "offline"):
        add("warn", "Embeddings", f"{emb} (offline) — lexical dedup/contradiction only; "
            f"semantic reversals (e.g. paraphrases) need hosted embeddings "
            f"(pip install leptin-hlp[hosted])")
    else:
        pkg = "voyageai" if emb.startswith("voyage") else "openai"
        have_pkg = importlib.util.find_spec(pkg) is not None
        key_env = "VOYAGE_API_KEY" if pkg == "voyageai" else "OPENAI_API_KEY"
        have_key = bool(os.environ.get(key_env))
        lvl = "ok" if (have_pkg and have_key) else "warn"
        add(lvl, "Embeddings", f"{emb} | SDK '{pkg}' {'installed' if have_pkg else 'MISSING (pip install leptin-hlp[hosted])'}"
            f" | {key_env} {'set' if have_key else 'not set → will fall back to local'}")

    llm = cfg.llm_model
    if llm in ("heuristic", "offline", "local"):
        add("ok", "Merge LLM", f"{llm} (offline)")
    else:
        pkg = "anthropic" if llm.startswith("claude") else "openai"
        have_pkg = importlib.util.find_spec(pkg) is not None
        key_env = "ANTHROPIC_API_KEY" if pkg == "anthropic" else "OPENAI_API_KEY"
        have_key = bool(os.environ.get(key_env))
        lvl = "ok" if (have_pkg and have_key) else "warn"
        add(lvl, "Merge LLM", f"{llm} | SDK '{pkg}' {'installed' if have_pkg else 'MISSING'}"
            f" | {key_env} {'set' if have_key else 'not set → heuristic fallback'}")

    # --- self-tuning / guardrail ---
    if mem is not None:
        tune = mem.diet_report("all").get("tuning")
        if tune:
            add("ok", "Self-tuning",
                f"enabled={tune['enabled']}, cycles={tune['cycles']}, "
                f"accepted={tune['accepted']}, llm_calls={tune['llm_calls']}")
        else:
            add("ok", "Self-tuning",
                f"enabled={cfg.self_tune_enabled} (no cycles yet)")
        run = mem.store.conn.execute(
            "SELECT passed, rolled_back FROM probe_runs ORDER BY id DESC LIMIT 1").fetchone()
        add("ok", "Guardrail", "no compactions yet" if not run else
            f"last: {'passed' if run['passed'] else 'failed'}"
            f"{', rolled back' if run['rolled_back'] else ''}")
        h = mem.health()
        hlvl = "ok" if h["grade"] in ("A", "B") else "warn"
        extra = f" · {h['conflicts']} conflict(s)" if h.get("conflicts") else ""
        add(hlvl, "Memory health",
            f"score {h['score']}/100 ({h['grade']}) · {h['active']} active · "
            f"stale {h['stale_rate']} · noise {h['noise_rate']}{extra}")
        if h.get("embedder_drift"):
            add("warn", "Embedder drift",
                "active memories carry >1 embedder (a hosted→local fallback?) — run `leptin reembed`")
        mem.close()

    # --- host wiring (is Leptin actually installed into the agent host?) ---
    hw = _host_wiring_status(getattr(args, "host", None) or "claude-code")
    add(hw["level"], "Host wiring", hw["detail"])

    icons = {"ok": "✓", "warn": "⚠", "error": "✗"}
    print("\n  Leptin doctor\n  " + "-" * 50)
    for level, name, detail in checks:
        print(f"  {icons[level]} {name:<12} {detail}")
    errors = sum(1 for c in checks if c[0] == "error")
    warns = sum(1 for c in checks if c[0] == "warn")
    status = "UNHEALTHY" if errors else ("OK (with warnings)" if warns else "HEALTHY")
    print("  " + "-" * 50)
    print(f"  {status}: {errors} error(s), {warns} warning(s)\n")
    if args.json:
        _print_json({"status": status, "errors": errors, "warnings": warns,
                     "checks": [{"level": l, "name": n, "detail": d} for l, n, d in checks]})
    return 1 if errors else 0


def cmd_tune(args) -> int:
    from leptin.api import Leptin

    with Leptin(args.db) as mem:
        if args.history:
            _print_json(mem.tune_history())
        elif args.rollback is not False:
            version = None if args.rollback is True else int(args.rollback)
            _print_json(mem.tune_rollback(version=version))
        else:
            _print_json(mem.tune(dry_run=args.dry_run))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="leptin",
        description="Leptin — personal, local-first memory infrastructure for your coding agent (no account, no subscription).",
    )
    p.add_argument("--version", action="version", version=f"leptin {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_db(sp):
        sp.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")

    sp = sub.add_parser("serve", help="Run the MCP server on stdio.")
    add_db(sp)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("bench", help="Run the reproducible token-savings benchmark.")
    sp.add_argument("--budget", type=int, default=1500, help="Recall token budget.")
    sp.add_argument("--top-k", type=int, default=10, help="Naive store's top-k dump size.")
    sp.add_argument("--dataset", help="Path to a real LoCoMo-format JSON (else bundled synthetic).")
    sp.add_argument("--limit", type=int, default=0, help="Max LoCoMo samples to load.")
    sp.add_argument("--embedding-model", default="local-hash",
                    help="e.g. text-embedding-3-small (needs leptin-hlp[hosted] + API key).")
    sp.add_argument("--llm-model", default="heuristic", help="e.g. gpt-4o-mini for merges.")
    sp.add_argument("--eval-contradiction", nargs="?", const=True, default=False,
                    help="Eval the contradiction detector (precision/recall/F1) on a labeled "
                         "JSONL (default: bundled set).")
    sp.add_argument("--json", action="store_true", help="Also print the raw result JSON.")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("init", help="Create a store and print the MCP config block.")
    add_db(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("report", help="Show the savings ledger (diet_report).")
    add_db(sp)
    sp.add_argument("--window", default="all", choices=["session", "7d", "all"])
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("remember", help="Store a memory.")
    add_db(sp)
    sp.add_argument("content")
    sp.add_argument("--subject")
    sp.add_argument("--source")
    sp.add_argument("--type", default="fact", choices=["fact", "procedural", "task", "lesson"],
                    help="Memory type (default fact). 'lesson' never decays.")
    sp.add_argument("--source-ref", help="Anchor: linear:ABC-123, spec:auth.md#flow, commit:sha.")
    sp.set_defaults(func=cmd_remember)

    sp = sub.add_parser("lesson", help="Store a never-forgotten lesson / anti-pattern.")
    add_db(sp)
    sp.add_argument("content")
    sp.add_argument("--subject")
    sp.set_defaults(func=cmd_lesson)

    sp = sub.add_parser("stale", help="Flag memories anchored to a changed source as stale.")
    add_db(sp)
    sp.add_argument("source_ref", help="e.g. linear:ABC-123 or spec:auth.md#flow")
    sp.set_defaults(func=cmd_stale)

    sp = sub.add_parser("feedback", help="Tell Leptin a recalled memory was useful/harmful.")
    add_db(sp)
    sp.add_argument("memory_id", nargs="+", help="One or more memory ids.")
    sp.add_argument("--harmful", action="store_true", help="Mark harmful (default: useful).")
    sp.set_defaults(func=cmd_feedback)

    sp = sub.add_parser("health", help="Memory-health score + drift flags.")
    add_db(sp)
    sp.set_defaults(func=cmd_health)

    sp = sub.add_parser("conflicts", help="List possible contradictions flagged for review.")
    add_db(sp)
    sp.set_defaults(func=cmd_conflicts)

    sp = sub.add_parser("superseded", help="List superseded memories + what replaced them.")
    add_db(sp)
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_superseded)

    sp = sub.add_parser("reembed", help="Re-embed active memories with the current embedder.")
    add_db(sp)
    sp.set_defaults(func=cmd_reembed)

    sp = sub.add_parser("demo", help="60-second demo: a reversed decision, naive store vs Leptin.")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("hook", help="Lifecycle-hook entrypoint for Claude Code / Codex.")
    add_db(sp)
    sp.add_argument("event", nargs="?", help="session-start | user-prompt-submit | stop | pre-compact")
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("connect", help="Wire Leptin into a host (--write edits the config; default prints it).")
    add_db(sp)
    sp.add_argument("host", nargs="?", default="claude-code", help="claude-code | codex")
    sp.add_argument("--write", action="store_true", help="Write the host config (backed up, idempotent).")
    sp.add_argument("--dry-run", action="store_true", help="Show what --write would change, without writing.")
    sp.add_argument("--minimal", action="store_true", help="Fewest hooks (SessionStart + Stop only).")
    sp.set_defaults(func=cmd_connect)

    sp = sub.add_parser("setup", help="One command an agent runs to install Leptin on itself (init + wire + verify).")
    add_db(sp)
    sp.add_argument("host", nargs="?", default="claude-code", help="claude-code | codex")
    sp.add_argument("--minimal", action="store_true", help="Fewest hooks (SessionStart + Stop only).")
    sp.add_argument("--dry-run", action="store_true", help="Show what it would do, without writing.")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("recall", help="Recall memories under a token budget.")
    add_db(sp)
    sp.add_argument("query")
    sp.add_argument("--budget", type=int)
    sp.add_argument("--k", type=int)
    sp.set_defaults(func=cmd_recall)

    sp = sub.add_parser("compact", help="Run guardrailed compaction.")
    add_db(sp)
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(func=cmd_compact)

    sp = sub.add_parser("inspect", help="Inspect a memory (glass box).")
    add_db(sp)
    sp.add_argument("--memory-id")
    sp.add_argument("--query")
    sp.set_defaults(func=cmd_inspect)

    sp = sub.add_parser("dashboard", help="Serve the local savings dashboard.")
    add_db(sp)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_dashboard)

    sp = sub.add_parser("doctor", help="Health check: store, schema, models, host wiring.")
    add_db(sp)
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--host", default="claude-code", help="Host to check wiring for (claude-code | codex).")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("tune", help="Self-tune the memory policy (offline, guardrailed).")
    add_db(sp)
    sp.add_argument("--dry-run", action="store_true", help="Preview the proposed change.")
    sp.add_argument("--rollback", nargs="?", const=True, default=False,
                    help="Undo the last tune, or restore a specific VERSION id.")
    sp.add_argument("--history", action="store_true", help="Show the evolution ledger.")
    sp.set_defaults(func=cmd_tune)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
