"""SQLite storage: schema, connection and migrations.

The store is a single global database (default ``~/.nimrod/work.db``) with two
logical layers:

* ``sessions`` -- one row per agent session, with the deterministic summary.
* ``events``   -- the normalized steps inside a session (prompts, edits,
  commands, notes). These are what make recall useful: you can search for a
  file path or a command, not just a session title.

Full-text search uses standalone FTS5 tables (no external-content triggers) so
that writes stay simple and re-ingestion is a plain delete+insert per session.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    agent         TEXT NOT NULL,
    session_id    TEXT NOT NULL,
    project_path  TEXT,
    project_name  TEXT,
    git_branch    TEXT,
    git_sha       TEXT,
    model         TEXT,
    title         TEXT,
    summary       TEXT,
    started_at    INTEGER,
    ended_at      INTEGER,
    duration_ms   INTEGER,
    source_path   TEXT,
    source_mtime  INTEGER,
    source_size   INTEGER,
    prompt_count  INTEGER NOT NULL DEFAULT 0,
    tool_count    INTEGER NOT NULL DEFAULT 0,
    file_count    INTEGER NOT NULL DEFAULT 0,
    additions     INTEGER NOT NULL DEFAULT 0,
    deletions     INTEGER NOT NULL DEFAULT 0,
    tokens_input  INTEGER NOT NULL DEFAULT 0,
    tokens_output INTEGER NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    content_hash  TEXT,
    raw           TEXT,
    ingested_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_agent   ON sessions(agent, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    ordinal    INTEGER NOT NULL DEFAULT 0,
    kind       TEXT NOT NULL,
    ts         INTEGER,
    text       TEXT,
    target     TEXT,
    meta       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_events_kind    ON events(kind);
CREATE INDEX IF NOT EXISTS idx_events_target  ON events(target);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    sid UNINDEXED, title, summary, project, tokenize='unicode61'
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
    session_id UNINDEXED, text, target, tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS embeddings (
    owner_type TEXT NOT NULL,
    owner_id   TEXT NOT NULL,
    model      TEXT NOT NULL,
    dim        INTEGER NOT NULL,
    vector     BLOB NOT NULL,
    PRIMARY KEY (owner_type, owner_id, model)
);

CREATE TABLE IF NOT EXISTS ingest_state (
    source     TEXT PRIMARY KEY,
    last_ts    INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen_files (
    path       TEXT PRIMARY KEY,
    mtime      REAL NOT NULL,
    size       INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def now_ms() -> int:
    return int(time.time() * 1000)


def get_state(conn: sqlite3.Connection, source: str) -> int:
    row = conn.execute(
        "SELECT last_ts FROM ingest_state WHERE source=?", (source,)
    ).fetchone()
    return int(row["last_ts"]) if row else 0


def set_state(conn: sqlite3.Connection, source: str, last_ts: int) -> None:
    conn.execute(
        """
        INSERT INTO ingest_state(source, last_ts, updated_at) VALUES(?,?,?)
        ON CONFLICT(source) DO UPDATE SET last_ts=excluded.last_ts,
                                          updated_at=excluded.updated_at
        """,
        (source, int(last_ts), now_ms()),
    )


def file_unchanged(conn: sqlite3.Connection, path: str, mtime: float, size: int) -> bool:
    row = conn.execute(
        "SELECT mtime, size FROM seen_files WHERE path=?", (path,)
    ).fetchone()
    if row is None:
        return False
    return abs(float(row["mtime"]) - float(mtime)) < 1e-6 and int(row["size"]) == int(size)


def mark_file(conn: sqlite3.Connection, path: str, mtime: float, size: int) -> None:
    conn.execute(
        """
        INSERT INTO seen_files(path, mtime, size, updated_at) VALUES(?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, size=excluded.size,
                                        updated_at=excluded.updated_at
        """,
        (path, float(mtime), int(size), now_ms()),
    )


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
