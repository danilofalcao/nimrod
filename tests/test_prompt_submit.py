"""Gated auto-recall on UserPromptSubmit."""

import io
import json
from types import SimpleNamespace

from nimrod import db
from nimrod.cli import _prompt_submit
from nimrod.config import Config
from nimrod.embeddings import Embedder
from nimrod.models import PROMPT, Event, ParsedSession
from nimrod.projects import project_name
from nimrod.store import Store


def _cfg(tmp_path):
    config = Config(home=tmp_path / "nimrod-home")
    config.ensure()
    return config


def _repo(tmp_path, name):
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


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


def _run(monkeypatch, capsys, config, payload, auto_recall=None):
    monkeypatch.delenv("NIMROD_AUTO_RECALL", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    if auto_recall is not None:
        monkeypatch.setenv("NIMROD_AUTO_RECALL", auto_recall)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    args = SimpleNamespace(project=payload.get("cwd"))
    _prompt_submit(args, config)
    return capsys.readouterr().out


def _context(out):
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_ignores_container_and_ancestor_projects(monkeypatch, capsys, tmp_path):
    home = tmp_path / "home"
    (home / ".git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    config = _cfg(tmp_path)
    _add_session(config, home, "home directory work")

    umbrella = _repo(tmp_path, "umbrella")
    nested = umbrella / "nested"
    (nested / ".git").mkdir(parents=True)
    _add_session(config, umbrella, "umbrella work")

    cwd = _repo(tmp_path, "unrelated")

    # A home directory is a container path even when it looks like a repo.
    assert _run(monkeypatch, capsys, config, {
        "prompt": "please check the files in my home directory",
        "cwd": str(cwd),
        "session_id": "sess-home",
    }) == ""

    # "umbrella" encloses the caller's cwd, so its brief is not injected.
    assert _run(monkeypatch, capsys, config, {
        "prompt": "let us revisit the umbrella layout",
        "cwd": str(nested),
        "session_id": "sess-umbrella",
    }) == ""


def test_recalls_only_real_unrelated_repositories(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "payments-api")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "fixed the payments parser")
    _add_session(config, cwd, "this must not leak")

    out = _run(monkeypatch, capsys, config, {
        "prompt": "remember how the payments-api work went?",
        "cwd": str(cwd),
        "session_id": "sess-1",
    })
    text = _context(out)
    assert "project 'payments-api'" in text
    assert "fixed the payments parser" in text
    assert "this must not leak" not in text


def test_plain_directories_are_never_recalled(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    plain = tmp_path / "scratch"
    plain.mkdir()
    _add_session(config, plain, "loose session in scratch")
    cwd = _repo(tmp_path, "current-repo")

    out = _run(monkeypatch, capsys, config, {
        "prompt": "look at the file under /tmp/scratch once more",
        "cwd": str(cwd),
        "session_id": "sess-1",
    })
    assert out == ""


def test_requires_a_session_id_to_be_deduplicated(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "payments-api")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "fixed the payments parser")

    payload = {"prompt": "remember how the payments-api work went?", "cwd": str(cwd)}
    assert _run(monkeypatch, capsys, config, payload) == ""


def test_recall_is_deduplicated_per_session(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "payments-api")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "fixed the payments parser")

    payload = {"prompt": "remember how the payments-api work went?",
               "cwd": str(cwd), "session_id": "sess-1"}
    assert _run(monkeypatch, capsys, config, payload) != ""
    assert _run(monkeypatch, capsys, config, payload) == ""
    payload["session_id"] = "sess-2"
    assert _run(monkeypatch, capsys, config, payload) != ""


def test_requires_every_word_of_the_project_name(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "data-pipeline")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "reshaped the pipeline")

    # A single shared word ("data"), e.g. from an absolute path, is not enough.
    assert _run(monkeypatch, capsys, config, {
        "prompt": "please open /srv/data/report.csv for me",
        "cwd": str(cwd),
        "session_id": "sess-1",
    }) == ""

    # Naming the whole project does recall it.
    out = _run(monkeypatch, capsys, config, {
        "prompt": "remember what we changed in data-pipeline?",
        "cwd": str(cwd),
        "session_id": "sess-2",
    })
    assert "project 'data-pipeline'" in _context(out)


def test_partial_hyphenated_name_is_not_enough(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "web-client")
    cwd = _repo(tmp_path, "api-server")
    _add_session(config, target, "rebuilt the client bundle")

    assert _run(monkeypatch, capsys, config, {
        "prompt": "the web keeps breaking again",
        "cwd": str(cwd),
        "session_id": "sess-1",
    }) == ""

    out = _run(monkeypatch, capsys, config, {
        "prompt": "remember how we fixed the web-client build?",
        "cwd": str(cwd),
        "session_id": "sess-2",
    })
    assert "project 'web-client'" in _context(out)


def test_auto_recall_can_be_disabled(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "payments-api")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "fixed the payments parser")

    out = _run(monkeypatch, capsys, config, {
        "prompt": "remember how the payments-api work went?",
        "cwd": str(cwd),
        "session_id": "sess-1",
    }, auto_recall="0")
    assert out == ""


def test_short_prompts_are_ignored(monkeypatch, capsys, tmp_path):
    config = _cfg(tmp_path)
    target = _repo(tmp_path, "payments-api")
    cwd = _repo(tmp_path, "web-client")
    _add_session(config, target, "fixed the payments parser")

    out = _run(monkeypatch, capsys, config, {
        "prompt": "payments",
        "cwd": str(cwd),
        "session_id": "sess-1",
    })
    assert out == ""
