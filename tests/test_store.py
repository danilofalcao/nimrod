from pathlib import Path

import numpy as np
import pytest

from nimrod import db
from nimrod.embeddings import Embedder, pack
from nimrod.models import COMMAND, Event, FILE_EDIT, PROMPT, ParsedSession
from nimrod.store import Store


@pytest.fixture()
def store(tmp_path):
    conn = db.connect(tmp_path / "work.db")
    yield Store(conn, Embedder(enabled=False))
    conn.close()


def _session(sid, project, prompt, file_path):
    p = ParsedSession(
        agent="claude",
        session_id=sid,
        project_path=project,
        project_name=Path(project).name,
        title=prompt,
        started_at=1000,
        ended_at=2000,
        prompt_count=1,
        tool_count=2,
    )
    p.events = [
        Event(kind=PROMPT, ordinal=1, ts=1000, text=prompt),
        Event(kind=FILE_EDIT, ordinal=2, ts=1500, target=file_path, text=file_path),
        Event(kind=COMMAND, ordinal=3, ts=1600, target="pytest -q", text="pytest -q"),
    ]
    p.summary = f"Intent: {prompt}\nChanged: {file_path}\nRan: pytest -q"
    return p


def test_write_search_and_context(store):
    store.write_session(_session("s1", "/home/me/alpha", "add login endpoint",
                                 "/home/me/alpha/auth.py"))
    store.write_session(_session("s2", "/home/me/beta", "fix css layout",
                                 "/home/me/beta/style.css"))

    assert store.stats()["sessions"] == 2

    by_prompt = store.search("login endpoint")
    assert [s["session_id"] for s in by_prompt] == ["s1"]

    by_path = store.search("auth.py")
    assert [s["session_id"] for s in by_path] == ["s1"]

    scoped = store.recent("/home/me/beta")
    assert [s["session_id"] for s in scoped] == ["s2"]

    ctx = store.context("/home/me/alpha")
    assert "add login endpoint" in ctx
    assert "auth.py" in ctx


def test_get_session_and_idempotent_write(store):
    p = _session("s1", "/home/me/alpha", "add login endpoint", "/home/me/alpha/auth.py")
    assert store.write_session(p) is True
    assert store.write_session(p) is False  # unchanged content hash

    data = store.get_session("s1")
    assert data is not None
    assert [e["kind"] for e in data["events"]] == ["prompt", "file_edit", "command"]


def test_record_note(store):
    sid = store.record_note("remember to bump the version", project="/home/me/alpha")
    notes = store.recent("/home/me/alpha")
    assert notes[0]["id"] == sid
    assert notes[0]["agent"] == "note"


def test_scope_is_exact_and_ignores_nested_projects(store):
    store.write_session(_session("parent", "/home/me", "work in the home bucket",
                                 "/home/me/notes.md"))
    store.write_session(_session("child", "/home/me/repo", "fix the widget",
                                 "/home/me/repo/widget.py"))
    store.write_session(_session("grandchild", "/home/me/repo/pkg", "tune packaging",
                                 "/home/me/repo/pkg/setup.py"))

    parent_scope = [s["session_id"] for s in store.recent("/home/me")]
    assert parent_scope == ["parent"]

    repo_scope = [s["session_id"] for s in store.recent("/home/me/repo")]
    assert repo_scope == ["child"]

    ctx = store.context("/home/me")
    assert "home bucket" in ctx
    assert "fix the widget" not in ctx
    assert "tune packaging" not in ctx


def test_scope_matches_project_name_case_insensitively(store):
    store.write_session(_session("s1", "/home/me/Alpha", "add login",
                                 "/home/me/Alpha/auth.py"))
    store.write_session(_session("s2", "/home/me/beta", "fix css",
                                 "/home/me/beta/style.css"))

    assert [s["session_id"] for s in store.recent("alpha")] == ["s1"]
    assert [s["session_id"] for s in store.recent("Alpha")] == ["s1"]
    # Names are no longer matched as substrings: "alp" must not match "Alpha".
    assert store.recent("alp") == []


def test_filtered_session_rows_applies_the_same_scope(store):
    store.write_session(_session("parent", "/home/me", "bucket", "/home/me/a.md"))
    store.write_session(_session("child", "/home/me/repo", "widget",
                                 "/home/me/repo/widget.py"))

    ids = ["claude:parent", "claude:child"]
    rows = store._filtered_session_rows(ids, "/home/me", None, None)
    assert set(rows) == {"claude:parent"}
    rows = store._filtered_session_rows(ids, None, None, None)
    assert set(rows) == set(ids)
    rows = store._filtered_session_rows(ids, "/home/me/repo", None, None)
    assert set(rows) == {"claude:child"}


class _FakeEmbedder:
    enabled = True
    model_name = "fake"

    def __init__(self, vector):
        self.vector = np.asarray(vector, dtype=np.float32)

    def embed(self, texts):
        if not texts:
            return None
        return np.stack([self.vector for _ in texts])


def test_semantic_search_respects_exact_scope(tmp_path):
    conn = db.connect(tmp_path / "sem.db")
    try:
        store = Store(conn, _FakeEmbedder([1.0, 0.0]))
        store.write_session(_session("parent", "/home/me", "bucket alpha",
                                     "/home/me/a.md"))
        store.write_session(_session("child", "/home/me/repo", "widget beta",
                                     "/home/me/repo/w.py"))
        for sid in ("claude:parent", "claude:child"):
            conn.execute("DELETE FROM embeddings WHERE owner_id=?", (sid,))
            conn.execute(
                "INSERT INTO embeddings(owner_type, owner_id, model, dim, vector) "
                "VALUES('session',?,?,?,?)",
                (sid, "fake", 2, pack([1.0, 0.0])),
            )

        # No lexical match, so only the semantic ranking (and its scoping) runs.
        scoped = store.search("zzzq", project="/home/me")
        assert [r["session_id"] for r in scoped] == ["parent"]

        everything = store.search("zzzq")
        assert {r["session_id"] for r in everything} == {"parent", "child"}
    finally:
        conn.close()
