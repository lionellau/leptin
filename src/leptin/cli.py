"""``leptin`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
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


def cmd_serve(args) -> int:
    from leptin.server import serve

    serve(args.db)
    return 0


def cmd_bench(args) -> int:
    from leptin import bench

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
        with Leptin(args.db) as mem:
            if event in ("session-start", "sessionstart", "user-prompt-submit", "userpromptsubmit"):
                query = payload.get("prompt") or payload.get("user_prompt")
                ctx = mem.session_context(query=query)
                text = ctx["text"]
                if text:
                    hook_event = "UserPromptSubmit" if "prompt" in event else "SessionStart"
                    print(json.dumps({"hookSpecificOutput": {
                        "hookEventName": hook_event, "additionalContext": text}}))
            elif event in ("post-tool-use", "posttooluse"):
                # Mistake→lesson loop: a failed tool call becomes a never-decaying,
                # auto-re-injected anti-pattern lesson (dedup prevents spam).
                lesson = _lesson_from_failure(payload)
                if lesson:
                    mem.capture_lesson(lesson)
            elif event in ("stop", "session-end", "sessionend", "pre-compact", "precompact"):
                mem.compact()  # decay + guardrailed prune; keeps the store clean
    except Exception:  # noqa: BLE001 — a hook must never break the host session
        pass
    return 0


def _lesson_from_failure(payload: dict) -> Optional[str]:
    """Heuristically turn a failed PostToolUse payload into an anti-pattern line."""
    resp = payload.get("tool_response") or payload.get("tool_result") or {}
    is_error = bool(payload.get("is_error") or (isinstance(resp, dict) and resp.get("is_error")))
    text = resp.get("error") if isinstance(resp, dict) else None
    text = text or (resp if isinstance(resp, str) else "") or str(payload.get("error") or "")
    if not is_error and "error" not in text.lower() and "fail" not in text.lower():
        return None
    tool = payload.get("tool_name") or payload.get("tool") or "a tool"
    cmd = ""
    ti = payload.get("tool_input") or {}
    if isinstance(ti, dict):
        cmd = ti.get("command") or ti.get("file_path") or ""
    summary = (text or "").strip().splitlines()[0][:160] if text else "it failed"
    detail = f" ({cmd})" if cmd else ""
    return f"Avoid: {tool}{detail} failed — {summary}"


def cmd_connect(args) -> int:
    """Print the host config to wire Leptin's lean MCP surface + lifecycle hooks."""
    db = args.db
    leptin = _leptin_command()
    hooks = {
        evt: [{"hooks": [{"type": "command", "command": f"{leptin} hook {hk} --db {db}"}]}]
        for evt, hk in (("SessionStart", "session-start"),
                        ("UserPromptSubmit", "user-prompt-submit"),
                        ("PostToolUse", "post-tool-use"),
                        ("Stop", "stop"), ("PreCompact", "pre-compact"))
    }
    block = {"mcpServers": {"leptin": {"command": leptin, "args": ["serve", "--db", db]}},
             "hooks": hooks}
    host = (args.host or "claude-code").lower()
    settings = "~/.claude/settings.json" if "claude" in host else "Codex settings (hooks + mcp)"
    print(f"Add this to your {host} config ({settings}):\n")
    print(json.dumps(block, indent=2))
    print("\nThe discipline (compact/decay/guardrail/self-tune) runs via the hooks +")
    print("the CLI/daemon — only `remember` and `recall` are exposed to the model.")
    print("Expose every tool with  LEPTIN_MCP_TOOLS=all.")
    return 0


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
        add("ok", "Embeddings", f"{emb} (offline, no API key needed)")
    else:
        pkg = "voyageai" if emb.startswith("voyage") else "openai"
        have_pkg = importlib.util.find_spec(pkg) is not None
        key_env = "VOYAGE_API_KEY" if pkg == "voyageai" else "OPENAI_API_KEY"
        have_key = bool(os.environ.get(key_env))
        lvl = "ok" if (have_pkg and have_key) else "warn"
        add(lvl, "Embeddings", f"{emb} | SDK '{pkg}' {'installed' if have_pkg else 'MISSING (pip install leptin-mcp[hosted])'}"
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
        mem.close()

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
        description="Leptin — the satiety hormone for agent memory.",
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
                    help="e.g. text-embedding-3-small (needs leptin-mcp[hosted] + API key).")
    sp.add_argument("--llm-model", default="heuristic", help="e.g. gpt-4o-mini for merges.")
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

    sp = sub.add_parser("hook", help="Lifecycle-hook entrypoint for Claude Code / Codex.")
    add_db(sp)
    sp.add_argument("event", nargs="?", help="session-start | user-prompt-submit | stop | pre-compact")
    sp.set_defaults(func=cmd_hook)

    sp = sub.add_parser("connect", help="Print host config to wire hooks + lean MCP.")
    add_db(sp)
    sp.add_argument("host", nargs="?", default="claude-code", help="claude-code | codex")
    sp.set_defaults(func=cmd_connect)

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

    sp = sub.add_parser("doctor", help="Health check: store, schema, models, hosted readiness.")
    add_db(sp)
    sp.add_argument("--json", action="store_true")
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
