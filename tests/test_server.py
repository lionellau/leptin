"""MCP server protocol surface (PRD 8.1)."""

from __future__ import annotations

import io
import json

from leptin.api import Leptin
from leptin.server import TOOLS, MCPServer


def _run(messages):
    mem = Leptin(":memory:")
    out = io.StringIO()
    srv = MCPServer(mem, out=out, err=io.StringIO())
    stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    srv.serve_forever(stdin=stdin)
    mem.close()
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _by_id(responses, mid):
    return next(r for r in responses if r.get("id") == mid)


def test_initialize_and_tool_list():
    resp = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ])
    init = _by_id(resp, 1)
    assert init["result"]["serverInfo"]["name"] == "leptin"
    assert init["result"]["protocolVersion"] == "2024-11-05"

    names = [t["name"] for t in _by_id(resp, 2)["result"]["tools"]]
    assert set(names) == {"remember", "recall", "compact", "forget", "restore",
                          "inspect", "diet_report", "self_tune"}
    assert len(TOOLS) == 8


def test_all_seven_tools_callable():
    resp = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "remember", "arguments": {"content": "dark mode", "subject": "p"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "recall", "arguments": {"query": "mode", "token_budget": 200}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "compact", "arguments": {"dry_run": True}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "inspect", "arguments": {"query": "mode"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
         "params": {"name": "diet_report", "arguments": {"window": "all"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "forget", "arguments": {"query": "mode"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
         "params": {"name": "restore", "arguments": {"memory_id": "nope"}}},
    ])
    for mid in range(1, 8):
        r = _by_id(resp, mid)
        assert "result" in r
        assert r["result"].get("isError") in (False, None)
        assert "structuredContent" in r["result"]


def test_unknown_tool_is_graceful_error():
    resp = _run([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "nope", "arguments": {}}},
    ])
    # MCP convention: unknown tool → tools/call result with isError, not a
    # JSON-RPC protocol error.
    result = _by_id(resp, 1)["result"]
    assert result["isError"] is True
    assert "Unknown tool" in result["content"][0]["text"]


def test_unknown_method_returns_jsonrpc_error():
    resp = _run([{"jsonrpc": "2.0", "id": 1, "method": "does/notexist"}])
    assert _by_id(resp, 1)["error"]["code"] == -32601


def test_malformed_line_does_not_crash():
    mem = Leptin(":memory:")
    out = io.StringIO()
    srv = MCPServer(mem, out=out, err=io.StringIO())
    stdin = io.StringIO("not json\n{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"ping\"}\n")
    srv.serve_forever(stdin=stdin)
    mem.close()
    responses = [json.loads(l) for l in out.getvalue().splitlines()]
    assert _by_id(responses, 9)["result"] == {}


def test_tool_schemas_have_required_fields():
    for t in TOOLS:
        assert "name" in t and "description" in t and "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"
