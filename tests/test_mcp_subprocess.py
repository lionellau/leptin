"""End-to-end MCP server test — spawns ``leptin serve`` as a real subprocess and
drives it over stdio exactly as Claude Code / Codex would."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))


class _Client:
    def __init__(self, db_path: str):
        env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "leptin", "serve", "--db", db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env=env,
        )

    def call(self, msg: dict):
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        if "id" in msg:
            line = self.proc.stdout.readline()
            return json.loads(line)
        return None

    def close(self) -> int:
        assert self.proc.stdin
        self.proc.stdin.close()
        return self.proc.wait(timeout=10)


@pytest.fixture
def client(tmp_path):
    c = _Client(str(tmp_path / "mcp.db"))
    c.call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
    c.call({"jsonrpc": "2.0", "method": "notifications/initialized"})
    yield c
    assert c.close() == 0


def test_subprocess_lists_seven_tools(client):
    resp = client.call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    assert names == {"remember", "recall", "compact", "forget", "restore",
                     "inspect", "diet_report"}


def test_subprocess_remember_and_recall_roundtrip(client):
    client.call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                 "params": {"name": "remember",
                            "arguments": {"content": "The deploy region is us-west-2.",
                                          "subject": "infra"}}})
    resp = client.call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "recall",
                                   "arguments": {"query": "which region for deploys?",
                                                 "token_budget": 300}}})
    sc = resp["result"]["structuredContent"]
    assert any("us-west-2" in m["content"] for m in sc["memories"])
    assert sc["tokens_used"] <= 300


def test_subprocess_persists_across_restart(tmp_path):
    db = str(tmp_path / "persist.db")
    c1 = _Client(db)
    c1.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    c1.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "remember",
                        "arguments": {"content": "Persistent fact across sessions.",
                                      "subject": "x"}}})
    assert c1.close() == 0

    c2 = _Client(db)  # new process, same db
    c2.call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    resp = c2.call({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "recall", "arguments": {"query": "persistent fact"}}})
    assert any("Persistent fact" in m["content"]
               for m in resp["result"]["structuredContent"]["memories"])
    assert c2.close() == 0
