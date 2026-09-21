"""Ingestion attributes a session to the repository that encloses its cwd."""

import json

from nimrod import db
from nimrod.config import Config
from nimrod.embeddings import Embedder
from nimrod.engine import ingest
from nimrod.store import Store


def _write_session(path, cwd):
    path.parent.mkdir(parents=True, exist_ok=True)
    session_id = path.stem.split("_")[-1]
    entries = [
        {"type": "session", "version": 3, "id": session_id,
         "timestamp": "2026-09-20T00:00:00.000Z", "cwd": cwd},
        {"type": "message", "id": "u1", "parentId": None,
         "timestamp": "2026-09-20T00:00:01.000Z",
         "message": {"role": "user", "content": "fix the widget",
                     "timestamp": 1758326401000}},
    ]
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def _config(tmp_path):
    return Config(
        home=tmp_path / "home",
        claude_root=tmp_path / "claude",
        codex_root=tmp_path / "codex",
        opencode_data=tmp_path / "opencode",
        pi_root=tmp_path / "pi",
    )


def _pi_session_file(tmp_path):
    return (tmp_path / "pi" / "sessions" / "--repo--"
            / "2026-09-20T00-00-00-000Z_abc123.jsonl")


def test_ingest_attributes_subdirectory_session_to_repo_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src"
    sub.mkdir()
    _write_session(_pi_session_file(tmp_path), str(sub))

    cfg = _config(tmp_path)
    report = ingest(cfg, force=True, semantic=False)
    assert report.changed == 1

    conn = db.connect(cfg.db_path)
    try:
        store = Store(conn, Embedder(enabled=False))
        stored = store.recent(str(repo))
        assert [s["session_id"] for s in stored] == ["abc123"]
        assert stored[0]["project_path"] == str(repo)
        assert stored[0]["project_name"] == "repo"
        # The subdirectory is no longer a project of its own.
        assert store.recent(str(sub)) == []
    finally:
        conn.close()


def test_ingest_project_filter_accepts_repo_root_with_trailing_slash(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write_session(_pi_session_file(tmp_path), str(repo / "src"))

    cfg = _config(tmp_path)
    report = ingest(cfg, force=True, semantic=False, project=str(repo) + "/")
    assert report.changed == 1


def test_ingest_outside_any_repo_keeps_the_literal_directory(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    _write_session(_pi_session_file(tmp_path), str(scratch))

    cfg = _config(tmp_path)
    assert ingest(cfg, force=True, semantic=False).changed == 1

    conn = db.connect(cfg.db_path)
    try:
        store = Store(conn, Embedder(enabled=False))
        stored = store.recent(str(scratch))
        assert [s["session_id"] for s in stored] == ["abc123"]
        assert stored[0]["project_name"] == "scratch"
    finally:
        conn.close()
