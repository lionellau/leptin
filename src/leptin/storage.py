"""SQLite storage for Leptin.

Holds memories (with embeddings), an append-only event log, the savings ledger,
and the guardrail's probe set. Vectors are stored as JSON and scored in Python —
fine for the thousands-of-entries scale of a personal/agent memory store, and it
keeps install dependency-free. (A ``sqlite-vec`` fast path is a future drop-in.)
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    subject         TEXT,
    content         TEXT NOT NULL,
    embedding       TEXT,                      -- JSON array of floats
    tokens          INTEGER NOT NULL DEFAULT 0,
    strength        REAL NOT NULL DEFAULT 1.0, -- base strength as of last_accessed_at
    created_at      REAL NOT NULL,
    last_accessed_at REAL NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active', -- active|superseded|quarantined|deleted
    superseded_by   TEXT,
    source_session  TEXT,
    provenance      TEXT,
    reversible_until REAL
);
CREATE INDEX IF NOT EXISTS idx_mem_status  ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject);

CREATE TABLE IF NOT EXISTS memory_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id   TEXT NOT NULL,
    type        TEXT NOT NULL,  -- create|merge|supersede|decay|forget|restore|recall_inject
    reason      TEXT,
    token_delta INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evt_mem ON memory_events(memory_id);

CREATE TABLE IF NOT EXISTS ledger (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    operation      TEXT NOT NULL,  -- remember|recall|compact|forget
    baseline_tokens INTEGER NOT NULL DEFAULT 0,
    actual_tokens   INTEGER NOT NULL DEFAULT 0,
    tokens_saved    INTEGER NOT NULL DEFAULT 0,
    model          TEXT,
    usd_saved      REAL NOT NULL DEFAULT 0.0,
    session_id     TEXT,
    detail         TEXT            -- JSON blob with op-specific extras
);
CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger(ts);

CREATE TABLE IF NOT EXISTS probes (
    id              TEXT PRIMARY KEY,
    question        TEXT NOT NULL,
    expected_fact   TEXT NOT NULL,
    source_memory_id TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS probe_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    trigger       TEXT NOT NULL,  -- compact|manual
    recall_before REAL,
    recall_after  REAL,
    passed        INTEGER,
    rolled_back   INTEGER
);

CREATE TABLE IF NOT EXISTS config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Self-tuning (v0.2): the evolution ledger.
CREATE TABLE IF NOT EXISTS config_versions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    knob        TEXT,
    old_value   TEXT,
    new_value   TEXT,
    direction   TEXT,            -- up | down | rollback
    accepted    INTEGER,
    reason      TEXT,
    parent_id   INTEGER,
    config_json TEXT             -- full Config snapshot for exact restore
);

CREATE TABLE IF NOT EXISTS tune_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    trigger          TEXT,
    cycle            INTEGER,
    recall_before    REAL,
    recall_after     REAL,
    reduction_before REAL,
    reduction_after  REAL,
    accepted         INTEGER,
    rolled_back      INTEGER,
    llm_calls        INTEGER NOT NULL DEFAULT 0,
    tune_tokens      INTEGER NOT NULL DEFAULT 0
);
"""

MEMORY_COLUMNS = [
    "id", "subject", "content", "embedding", "tokens", "strength",
    "created_at", "last_accessed_at", "access_count", "status",
    "superseded_by", "source_session", "provenance", "reversible_until",
]


def _new_id() -> str:
    return uuid.uuid4().hex


# --- schema migrations -------------------------------------------------------
SCHEMA_VERSION = 2


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return column in {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if not _has_column(conn, table, column):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migration_1(conn: sqlite3.Connection) -> None:
    """Columns from the 0.1.x line (idempotent: no-ops on a fresh DB)."""
    _add_column(conn, "memories", "superseded_by", "TEXT")
    _add_column(conn, "memories", "reversible_until", "REAL")
    _add_column(conn, "memories", "provenance", "TEXT")
    _add_column(conn, "ledger", "detail", "TEXT")


def _migration_2(conn: sqlite3.Connection) -> None:
    """v1.0 self-tuning: the tables are created by SCHEMA (IF NOT EXISTS); this
    establishes the version boundary so older DBs are recognised and upgraded."""
    return None


_MIGRATIONS = {1: _migration_1, 2: _migration_2}


class Store:
    """Thin, explicit data-access layer over SQLite. No business logic here."""

    def __init__(self, path: str = ":memory:", clock: Optional[Callable[[], float]] = None):
        self.path = path
        self._clock = clock or time.time
        # Parsed-embedding cache (id -> list[float]) so recall over a large store
        # doesn't re-parse every memory's JSON vector on every call.
        self._emb_cache: dict[str, list[float]] = {}
        # check_same_thread=False so the MCP server (single-threaded loop) and
        # the HTTP dashboard can share a connection safely under the GIL.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # Autocommit mode: normal writes persist immediately; guardrailed
        # compaction manages its own explicit BEGIN/COMMIT/ROLLBACK.
        self.conn.isolation_level = None
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        # Concurrency: wait up to 5s for a lock instead of erroring immediately,
        # so multiple processes (e.g. MCP server + dashboard + CLI) coexist.
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self._migrate()

    # --- schema versioning / migrations ------------------------------------
    def _migrate(self) -> None:
        """Bring an existing database up to the current schema.

        The base ``SCHEMA`` (CREATE TABLE IF NOT EXISTS) guarantees every table
        exists. Versioned migrations below handle column additions / transforms
        on databases created by older Leptin versions, tracked via
        ``PRAGMA user_version``. Each migration is idempotent.
        """
        current = self.conn.execute("PRAGMA user_version").fetchone()[0]
        for version, migrate in sorted(_MIGRATIONS.items()):
            if current < version:
                migrate(self.conn)
                self.conn.execute(f"PRAGMA user_version={version}")
        # A brand-new DB jumps straight to the latest version.
        if current < SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def now(self) -> float:
        return self._clock()

    def close(self) -> None:
        self.conn.close()

    # --- transactions (used by guardrailed compaction) ---
    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    # --- memories ---
    def add_memory(
        self,
        content: str,
        embedding: list[float],
        tokens: int,
        subject: Optional[str] = None,
        strength: float = 1.0,
        source_session: Optional[str] = None,
        provenance: Optional[str] = None,
        memory_id: Optional[str] = None,
    ) -> dict[str, Any]:
        mid = memory_id or _new_id()
        now = self.now()
        self.conn.execute(
            """INSERT INTO memories
               (id, subject, content, embedding, tokens, strength, created_at,
                last_accessed_at, access_count, status, source_session, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mid, subject, content, json.dumps(embedding), tokens, strength,
                now, now, 0, "active", source_session, provenance,
            ),
        )
        self._emb_cache[mid] = list(embedding)  # seed the cache
        return self.get_memory(mid)  # type: ignore[return-value]

    def _parse_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Row → dict, using the embedding cache to avoid re-parsing JSON vectors."""
        d = dict(row)
        mid = d["id"]
        cached = self._emb_cache.get(mid)
        if cached is not None:
            d["embedding"] = cached
        else:
            emb: list[float] = []
            if d.get("embedding"):
                try:
                    emb = json.loads(d["embedding"])
                except (TypeError, json.JSONDecodeError):
                    emb = []
            d["embedding"] = emb
            self._emb_cache[mid] = emb
        return d

    def get_memory(self, memory_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM memories WHERE id=?", (memory_id,)
        ).fetchone()
        return self._parse_row(row) if row else None

    def update_memory(self, memory_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols = []
        vals: list[Any] = []
        for k, v in fields.items():
            if k == "embedding" and v is not None and not isinstance(v, str):
                v = json.dumps(v)
            cols.append(f"{k}=?")
            vals.append(v)
        vals.append(memory_id)
        self.conn.execute(
            f"UPDATE memories SET {', '.join(cols)} WHERE id=?", vals
        )
        if "embedding" in fields:
            self._emb_cache.pop(memory_id, None)  # invalidate stale vector

    def list_memories(
        self, status: Optional[str] = "active", subject: Optional[str] = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM memories"
        clauses = []
        args: list[Any] = []
        if status is not None:
            clauses.append("status=?")
            args.append(status)
        if subject is not None:
            clauses.append("subject=?")
            args.append(subject)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY created_at"
        return [self._parse_row(r) for r in self.conn.execute(q, args).fetchall()]

    def count_memories(self, status: Optional[str] = "active") -> int:
        if status is None:
            return self.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        return self.conn.execute(
            "SELECT COUNT(*) FROM memories WHERE status=?", (status,)
        ).fetchone()[0]

    # --- events ---
    def add_event(
        self, memory_id: str, type: str, reason: str = "", token_delta: int = 0
    ) -> None:
        self.conn.execute(
            """INSERT INTO memory_events (memory_id, type, reason, token_delta, created_at)
               VALUES (?,?,?,?,?)""",
            (memory_id, type, reason, token_delta, self.now()),
        )

    def events_for(self, memory_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM memory_events WHERE memory_id=? ORDER BY id", (memory_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # --- ledger ---
    def add_ledger(
        self,
        operation: str,
        baseline_tokens: int,
        actual_tokens: int,
        tokens_saved: int,
        model: str,
        usd_saved: float,
        session_id: Optional[str],
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO ledger
               (ts, operation, baseline_tokens, actual_tokens, tokens_saved,
                model, usd_saved, session_id, detail)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                self.now(), operation, baseline_tokens, actual_tokens, tokens_saved,
                model, usd_saved, session_id,
                json.dumps(detail) if detail else None,
            ),
        )

    def ledger_rows(self, since: Optional[float] = None) -> list[dict[str, Any]]:
        if since is None:
            rows = self.conn.execute("SELECT * FROM ledger ORDER BY ts").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM ledger WHERE ts>=? ORDER BY ts", (since,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("detail"):
                try:
                    d["detail"] = json.loads(d["detail"])
                except (TypeError, json.JSONDecodeError):
                    pass
            out.append(d)
        return out

    # --- probes ---
    def add_probe(
        self, question: str, expected_fact: str, source_memory_id: Optional[str] = None
    ) -> str:
        pid = _new_id()
        self.conn.execute(
            """INSERT INTO probes (id, question, expected_fact, source_memory_id, created_at)
               VALUES (?,?,?,?,?)""",
            (pid, question, expected_fact, source_memory_id, self.now()),
        )
        return pid

    def list_probes(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.conn.execute("SELECT * FROM probes ORDER BY created_at").fetchall()
        ]

    def clear_probes(self) -> None:
        self.conn.execute("DELETE FROM probes")

    def add_probe_run(
        self,
        trigger: str,
        recall_before: float,
        recall_after: float,
        passed: bool,
        rolled_back: bool,
    ) -> None:
        self.conn.execute(
            """INSERT INTO probe_runs (ts, trigger, recall_before, recall_after, passed, rolled_back)
               VALUES (?,?,?,?,?,?)""",
            (self.now(), trigger, recall_before, recall_after, int(passed), int(rolled_back)),
        )

    # --- self-tuning: evolution ledger ---
    def add_config_version(
        self, knob: Optional[str], old_value: Any, new_value: Any, direction: str,
        accepted: bool, reason: str, config_json: dict[str, Any],
        parent_id: Optional[int] = None,
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO config_versions
               (ts, knob, old_value, new_value, direction, accepted, reason, parent_id, config_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (self.now(), knob, json.dumps(old_value), json.dumps(new_value), direction,
             int(accepted), reason, parent_id, json.dumps(config_json)),
        )
        return int(cur.lastrowid)

    def latest_config_version(self, accepted_only: bool = True) -> Optional[dict[str, Any]]:
        q = "SELECT * FROM config_versions"
        if accepted_only:
            q += " WHERE accepted=1"
        q += " ORDER BY id DESC LIMIT 1"
        row = self.conn.execute(q).fetchone()
        return _decode_version(row) if row else None

    def get_config_version(self, version_id: int) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM config_versions WHERE id=?", (version_id,)
        ).fetchone()
        return _decode_version(row) if row else None

    def list_config_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM config_versions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_decode_version(r) for r in rows]

    def add_tune_run(self, **fields: Any) -> None:
        cols = ["trigger", "cycle", "recall_before", "recall_after",
                "reduction_before", "reduction_after", "accepted", "rolled_back",
                "llm_calls", "tune_tokens"]
        vals = [self.now()] + [fields.get(c) for c in cols]
        # Coerce booleans.
        self.conn.execute(
            f"INSERT INTO tune_runs (ts, {', '.join(cols)}) VALUES ({','.join(['?'] * (len(cols) + 1))})",
            tuple(int(v) if isinstance(v, bool) else v for v in vals),
        )

    def list_tune_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM tune_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]

    def count_tune_runs(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM tune_runs").fetchone()[0]

    # --- config ---
    def save_config(self, data: dict[str, Any]) -> None:
        for k, v in data.items():
            self.conn.execute(
                "INSERT INTO config (key, value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, json.dumps(v)),
            )

    def load_config(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for r in self.conn.execute("SELECT key, value FROM config").fetchall():
            try:
                out[r["key"]] = json.loads(r["value"])
            except (TypeError, json.JSONDecodeError):
                out[r["key"]] = r["value"]
        return out


def _row_to_memory(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    if d.get("embedding"):
        try:
            d["embedding"] = json.loads(d["embedding"])
        except (TypeError, json.JSONDecodeError):
            d["embedding"] = []
    else:
        d["embedding"] = []
    return d


def _decode_version(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for k in ("old_value", "new_value", "config_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, json.JSONDecodeError):
                pass
    return d
