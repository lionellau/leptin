"""v1.4 — agent-installable host wiring + offline-tier hardening.

These cover the riskiest new code: mutating the user's host settings.json so an
agent can install Leptin on itself, and the offline recall floor/hybrid that make
the free local tier usable without a key."""

from __future__ import annotations

import json

from leptin import cli
from leptin.config import Config


# --- host-config write/merge/backup/idempotency -----------------------------
def test_connect_write_merges_without_clobbering(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
    }))
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(settings))
    db = str(tmp_path / "m.db")

    assert cli.main(["connect", "claude-code", "--db", db, "--write"]) == 0
    cfg = json.loads(settings.read_text())
    # unrelated server + existing hook preserved; leptin added
    assert "other" in cfg["mcpServers"] and "leptin" in cfg["mcpServers"]
    assert len(cfg["hooks"]["SessionStart"]) == 2
    assert {"SessionStart", "UserPromptSubmit", "PostToolUse", "Stop", "PreCompact"} <= set(cfg["hooks"])
    # a backup was written before the change
    assert any(p.name.startswith("settings.json.leptin-bak-") for p in tmp_path.iterdir())


def test_connect_write_is_idempotent(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(settings))
    db = str(tmp_path / "m.db")
    cli.main(["connect", "claude-code", "--db", db, "--write"])
    cli.main(["connect", "claude-code", "--db", db, "--write"])
    cfg = json.loads(settings.read_text())
    assert len(cfg["hooks"]["SessionStart"]) == 1   # no duplicate leptin hook
    assert list(cfg["mcpServers"]).count("leptin") == 1


def test_connect_refuses_malformed_settings(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("not json {")
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(settings))
    rc = cli.main(["connect", "claude-code", "--db", str(tmp_path / "m.db"), "--write"])
    assert rc == 1
    assert settings.read_text() == "not json {"   # left untouched


def test_connect_minimal_writes_two_hooks(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(settings))
    cli.main(["connect", "claude-code", "--db", str(tmp_path / "m.db"), "--write", "--minimal"])
    cfg = json.loads(settings.read_text())
    assert set(cfg["hooks"]) == {"SessionStart", "Stop"}


def test_setup_wires_store_and_config(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(settings))
    db = str(tmp_path / "m.db")
    cli.main(["setup", "claude-code", "--db", db])
    cfg = json.loads(settings.read_text())
    assert "leptin" in cfg["mcpServers"]
    status = cli._host_wiring_status("claude-code")
    assert status["level"] in ("ok", "warn")  # ok if the binary resolves on PATH


def test_doctor_reports_host_wiring(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_host_settings_path", lambda host: str(tmp_path / "absent.json"))
    cli.main(["doctor", "--db", str(tmp_path / "m.db")])
    out = capsys.readouterr().out
    assert "Host wiring" in out and "not wired" in out


# --- offline-tier hardening --------------------------------------------------
def test_offline_floor_drops_no_match_query(mem):
    mem.remember("The deploy region is us-west-2.", subject="infra")
    res = mem.recall("zzz qqq vvv wholly unrelated gibberish tokens")
    assert res["memories"] == []   # offline absolute floor → noise returns nothing


def test_offline_hybrid_recalls_word_overlap(make_mem):
    # Hybrid (max of hash-cosine and word overlap) finds a clear lexical match the
    # collision-prone hash vector alone can rank low.
    m = make_mem(Config())  # offline default, hybrid on
    m.remember("The primary datastore is PostgreSQL.", subject="db")
    res = m.recall("which datastore do we use")
    assert any("PostgreSQL" in x["content"] for x in res["memories"])
