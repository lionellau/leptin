"""Leptin MCP server (stdio, JSON-RPC 2.0).

Dependency-free: speaks the Model Context Protocol over newline-delimited
JSON-RPC on stdin/stdout so Claude Code / Codex can connect with a standard
config block and zero install friction (``uvx leptin-hlp serve``). Diagnostics
go to stderr; only protocol messages go to stdout.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import Any, Callable, Optional

from leptin import __version__
from leptin.api import Leptin

PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# --- Tool schemas (advertised via tools/list) --------------------------------
TOOLS: list[dict[str, Any]] = [
    {
        "name": "remember",
        "description": (
            "Save a durable fact, decision, or lesson the moment your human states it "
            "— so future-you doesn't re-learn it or act on the old version. If it "
            "contradicts something you stored before, the NEW truth wins automatically "
            "(the old one is kept, reversible). Use mtype='lesson' for a mistake you "
            "must never repeat (never forgotten); 'task' for ticket-scoped notes that "
            "fade. source_ref anchors it to a spec/ticket so it's flagged when that changes."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The thing to remember."},
                "subject": {"type": "string", "description": "Optional topic/subject grouping."},
                "source": {"type": "string", "description": "Optional provenance note."},
                "mtype": {"type": "string", "enum": ["fact", "procedural", "task", "lesson"],
                          "description": "Memory type (default fact). 'lesson' never decays."},
                "source_ref": {"type": "string",
                               "description": "Anchor: e.g. linear:ABC-123, spec:auth.md#flow, commit:sha."},
            },
            "required": ["content"],
        },
    },
    {
        "name": "recall",
        "description": (
            "Retrieve what past-you knows — call this BEFORE acting on a project "
            "decision (package manager, framework, region, auth, conventions). What "
            "comes back is the CURRENT resolved truth: reversed decisions are already "
            "removed, so trust it over your own assumptions. Packed under a token "
            "budget (not a top-k dump)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to recall."},
                "token_budget": {"type": "integer", "description": "Max tokens to inject."},
                "k": {"type": "integer", "description": "Candidate pool size."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "compact",
        "description": (
            "Run guardrailed compaction: decay-prune weak memories and merge "
            "duplicates. A recall guardrail re-checks the store afterwards and "
            "auto-rolls-back any prune that would hurt recall. Use dry_run to preview."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"dry_run": {"type": "boolean", "description": "Preview only."}},
        },
    },
    {
        "name": "forget",
        "description": (
            "Soft-delete a memory by id or by query. Forgotten memories are "
            "quarantined (reversible), never hard-deleted, and can be restored."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "query": {"type": "string"},
            },
        },
    },
    {
        "name": "restore",
        "description": "Restore a forgotten/quarantined memory back to active.",
        "inputSchema": {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "inspect",
        "description": (
            "Glass-box view of a memory by id or query: content, provenance, "
            "current strength, status, and full event history."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string"},
                "query": {"type": "string"},
            },
        },
    },
    {
        "name": "diet_report",
        "description": (
            "Memory-health readout for a window (session | 7d | all): is your memory "
            "staying correct and lean — contradictions resolved, stale facts flagged, "
            "nothing silently dropped — not just growing. Includes the audit + savings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "window": {"type": "string", "enum": ["session", "7d", "all"]},
            },
        },
    },
    {
        "name": "record_feedback",
        "description": (
            "Close the loop on recalled memories: mark them 'useful' (it helped — "
            "reinforces, and reverses one prior harmful mark) or 'harmful' (it "
            "misled — down-weights; repeated harm also flags it for review). This "
            "is the only signal that proves a memory actually helped, so the store "
            "gets more relevant with use. Pass the memory_ids you acted on."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_ids": {"type": "array", "items": {"type": "string"},
                               "description": "Memory ids (from recall) to score."},
                "signal": {"type": "string", "enum": ["useful", "harmful"],
                           "description": "Outcome of using those memories."},
            },
            "required": ["memory_ids", "signal"],
        },
    },
    {
        "name": "self_tune",
        "description": (
            "Self-tune the memory policy: replay this store's data under candidate "
            "configs and commit a change only if held-out evals prove a net win "
            "(more savings, no recall loss) — else leave it unchanged. Offline, "
            "zero LLM calls. Use dry_run to preview the proposed change."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"dry_run": {"type": "boolean", "description": "Preview only."}},
        },
    },
]

# Lean default surface: only what the *model* should call lives in the tool list
# (every tool schema is a standing per-request token tax). Discipline tools
# (compact/forget/restore/inspect/diet_report/self_tune) run via hooks/CLI, not
# the model. `record_feedback` is opt-in: it's model-facing but off by default to
# keep the surface to two — enable it (and the rest) via LEPTIN_MCP_TOOLS.
LEAN_TOOLS = {"remember", "recall"}


def visible_tools() -> list[dict[str, Any]]:
    """Resolve the advertised tool surface from ``LEPTIN_MCP_TOOLS``:
    unset/``lean`` → the two model tools; ``all`` → everything; or an explicit
    comma list (e.g. ``remember,recall,record_feedback``) for fine control."""
    spec = os.environ.get("LEPTIN_MCP_TOOLS", "lean").strip().lower()
    if spec == "all":
        return TOOLS
    if spec in ("", "lean"):
        names = LEAN_TOOLS
    else:
        names = {n.strip() for n in spec.split(",") if n.strip()}
    return [t for t in TOOLS if t["name"] in names]


class MCPServer:
    def __init__(self, mem: Leptin, out=None, err=None):
        self.mem = mem
        self.out = out or sys.stdout
        self.err = err or sys.stderr
        self.initialized = False
        self.tools = visible_tools()
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "remember": lambda a: mem.remember(a.get("content", ""), a.get("subject"),
                                               a.get("source"), a.get("mtype", "fact"),
                                               a.get("source_ref")),
            "recall": lambda a: mem.recall(a.get("query", ""), a.get("token_budget"), a.get("k")),
            "compact": lambda a: mem.compact(bool(a.get("dry_run", False))),
            "forget": lambda a: mem.forget(a.get("memory_id"), a.get("query")),
            "restore": lambda a: mem.restore(a.get("memory_id", "")),
            "inspect": lambda a: mem.inspect(a.get("memory_id"), a.get("query")),
            "diet_report": lambda a: mem.diet_report(a.get("window", "session")),
            "record_feedback": lambda a: mem.record_feedback(
                a.get("memory_ids", []), a.get("signal", "useful")),
            "self_tune": lambda a: mem.tune(bool(a.get("dry_run", False))),
        }

    # --- io ---
    def _log(self, *args: Any) -> None:
        print("[leptin]", *args, file=self.err, flush=True)

    def _send(self, obj: dict[str, Any]) -> None:
        self.out.write(json.dumps(obj) + "\n")
        self.out.flush()

    def _result(self, msg_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def _error(self, msg_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": code, "message": message}})

    # --- dispatch ---
    def handle(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            # Negotiate: honour the client's version if we support it, else
            # advertise our default supported version.
            requested = params.get("protocolVersion")
            proto = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
            self._result(msg_id, {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "leptin", "version": __version__},
                "instructions": (
                    "This is YOUR persistent memory across sessions, on this machine. "
                    "Before acting on a project decision (package manager, framework, "
                    "region, auth, conventions), call `recall` first — your human may "
                    "have reversed it in a session you don't remember, and `recall` "
                    "returns only the CURRENT truth. When your human states a durable "
                    "fact, decision, or a mistake to avoid, call `remember` — the newer "
                    "truth automatically supersedes any older conflicting one. It's "
                    "local, no account, no API key required. See AGENTS.md to install "
                    "yourself into a new project."
                ),
            })
        elif method in ("notifications/initialized", "initialized"):
            self.initialized = True
        elif method == "ping":
            if not is_notification:
                self._result(msg_id, {})
        elif method == "tools/list":
            self._result(msg_id, {"tools": self.tools})
        elif method == "tools/call":
            self._call_tool(msg_id, params)
        elif method in ("notifications/cancelled",):
            pass  # ignore
        else:
            if not is_notification:
                self._error(msg_id, -32601, f"Method not found: {method}")

    def _call_tool(self, msg_id: Any, params: dict[str, Any]) -> None:
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = self._handlers.get(name)
        if handler is None:
            # MCP convention: tools/call surfaces tool-level problems as a result
            # with isError=true (not a JSON-RPC protocol error).
            self._result(msg_id, {
                "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                "isError": True,
            })
            return
        # Graceful degradation: a tool failure becomes a tool-level error, never
        # a crashed server or a thrown exception to the agent.
        try:
            result = handler(args)
            self._result(msg_id, {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                "structuredContent": result,
                "isError": False,
            })
        except Exception as exc:  # noqa: BLE001
            self._log("tool error:", name, repr(exc))
            self._log(traceback.format_exc())
            self._result(msg_id, {
                "content": [{"type": "text", "text": f"Leptin tool '{name}' failed: {exc}"}],
                "isError": True,
            })

    # --- main loop ---
    def serve_forever(self, stdin=None) -> None:
        stdin = stdin or sys.stdin
        self._log(f"v{__version__} ready on stdio (db={self.mem.store.path})")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                self._log("skipping non-JSON line")
                continue
            try:
                self.handle(msg)
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                self._log("handler crash:", repr(exc))
                if "id" in msg:
                    self._error(msg.get("id"), -32603, f"Internal error: {exc}")
        self._log("stdin closed, shutting down")


def serve(db_path: str, config: Optional[Any] = None) -> None:
    mem = Leptin(db_path, config=config)
    MCPServer(mem).serve_forever()
