from pathlib import Path

import pytest

from nimrod import db
from nimrod.embeddings import Embedder
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
