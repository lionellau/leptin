"""``leptin`` command-line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from leptin import __version__

DEFAULT_DB = os.path.expanduser("~/.leptin/memory.db")


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _mcp_block(db_path: str) -> str:
    block = {
        "mcpServers": {
            "leptin": {
                "command": "leptin",
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

    r = bench.main(budget=args.budget, naive_top_k=args.top_k)
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
        _print_json(mem.remember(args.content, subject=args.subject, source=args.source))
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
    sp.set_defaults(func=cmd_remember)

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

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
