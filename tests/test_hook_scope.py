"""The session-start hook only emits a project brief inside a repository."""

from types import SimpleNamespace

import pytest

from nimrod import cli, db
from nimrod.config import Config
from nimrod.embeddings import Embedder
from nimrod.models import PROMPT, Event, ParsedSession
from nimrod.projects import project_name
from nimrod.store import Store


@pytest.fixture()
def no_ingest(monkeypatch):
    monkeypatch.setattr(cli, "run_ingest", lambda *args, **kwargs: None)


def _add_session(config, project, title):
    conn = db.connect(config.db_path)
    try:
        store = Store(conn, Embedder(enabled=False))
        p = ParsedSession(
            agent="pi",
            session_id=title,
            project_path=str(project),
            project_name=project_name(str(project)),
            title=title,
            started_at=1000,
            ended_at=2000,
            prompt_count=1,
        )
        p.events = [Event(kind=PROMPT, ordinal=1, ts=1000, text=title)]
        p.summary = f"Intent: {title}"
        store.write_session(p, force=True)
    finally:
        conn.close()


def _args(tmp_path, project, event="session-start"):
    return SimpleNamespace(
        event=event,
        agent="pi",
        project=str(project),
        limit=6,
        semantic=False,
        home=str(tmp_path / "home"),
    )


def test_brief_is_emitted_inside_a_repo_and_skipped_outside(tmp_path, capsys,
                                                            no_ingest):
    config = Config(home=tmp_path / "home")
    config.ensure()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _add_session(config, repo, "fix the widget")

    cli.cmd_hook(_args(tmp_path, repo))
    out = capsys.readouterr().out
    assert "worklog for repo" in out
    assert "fix the widget" in out

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    cli.cmd_hook(_args(tmp_path, scratch))
    out = capsys.readouterr().out
    assert "worklog for" not in out
    assert "fix the widget" not in out


def test_brief_uses_the_repo_root_for_subdirectories(tmp_path, capsys, no_ingest):
    config = Config(home=tmp_path / "home")
    config.ensure()
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)
    _add_session(config, repo, "fix the widget")

    cli.cmd_hook(_args(tmp_path, sub))
    out = capsys.readouterr().out
    assert "worklog for repo" in out
    assert "fix the widget" in out


def test_session_end_is_silent(tmp_path, capsys, no_ingest):
    cli.cmd_hook(_args(tmp_path, tmp_path, event="session-end"))
    assert capsys.readouterr().out == ""
