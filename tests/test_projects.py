"""Project identity helpers."""

from nimrod.projects import (
    find_project_root,
    in_dot_directory,
    is_auto_recall_candidate,
    is_container_path,
    normalize,
    project_name,
    resolve_project,
)


def test_normalize_expands_and_collapses():
    assert normalize(None) is None
    assert normalize("") is None
    assert normalize("/home/x/../y/") == "/home/y"


def test_project_name_never_contains_a_separator(monkeypatch, tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert project_name(str(home)) == "home"
    assert project_name("/") == "root"
    assert project_name(str(home / "src" / "nimrod")) == "nimrod"
    for value in (str(home), "/", str(home / "src" / "nimrod")):
        assert "/" not in (project_name(value) or "")


def test_is_container_path(monkeypatch, tmp_path):
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert is_container_path(str(home))
    assert is_container_path(str(home.parent))
    assert is_container_path("/tmp")
    assert is_container_path("/")
    assert not is_container_path(str(home / "Projects" / "nimrod"))
    assert not is_container_path(None)


def test_in_dot_directory():
    assert in_dot_directory("/home/x/.config/tool")
    assert in_dot_directory("/home/x/.codex")
    assert not in_dot_directory("/home/x/src/nimrod")
    assert not in_dot_directory("/tmp/pytest-1/test_foo0")


def test_find_project_root_finds_nearest_vcs(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    assert find_project_root(str(repo)) == str(repo)
    assert find_project_root(str(sub)) == str(repo)


def test_find_project_root_prefers_vcs_over_manifest(tmp_path):
    mono = tmp_path / "mono"
    (mono / ".git").mkdir(parents=True)
    pkg = mono / "pkg"
    pkg.mkdir()
    (pkg / "package.json").write_text("{}", encoding="utf-8")

    assert find_project_root(str(pkg)) == str(mono)


def test_find_project_root_uses_manifest_without_vcs(tmp_path):
    lib = tmp_path / "lib"
    sub = lib / "sub"
    sub.mkdir(parents=True)
    (lib / "pyproject.toml").write_text("", encoding="utf-8")

    assert find_project_root(str(sub)) == str(lib)


def test_find_project_root_none_for_plain_and_relative(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert find_project_root(str(plain)) is None
    assert find_project_root("nimrod") is None
    assert find_project_root(None) is None


def test_resolve_project_canonicalizes_to_root(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src"
    sub.mkdir()

    assert resolve_project(str(sub)) == str(repo)
    assert resolve_project("nimrod") == "nimrod"
    assert resolve_project(None) is None


def test_auto_recall_candidate_rejects_buckets_and_ancestors(tmp_path):
    repo_a = tmp_path / "alpha"
    repo_b = tmp_path / "beta"
    nested = repo_a / "nested"
    for path in (repo_a, repo_b, nested):
        (path / ".git").mkdir(parents=True)

    cwd = str(repo_b)

    # A sibling repository is a valid target.
    assert is_auto_recall_candidate(str(repo_a), cwd)
    # The repository the agent is already inside is not.
    assert not is_auto_recall_candidate(str(repo_b), cwd)
    assert not is_auto_recall_candidate(str(repo_a), str(nested))
    # Plain directories and container paths are not projects.
    assert not is_auto_recall_candidate(str(tmp_path / "scratch"), cwd)
    assert not is_auto_recall_candidate("/tmp", cwd)
    # Hidden directories are configuration, not source.
    dot_repo = tmp_path / ".cache" / "proj"
    (dot_repo / ".git").mkdir(parents=True)
    assert not is_auto_recall_candidate(str(dot_repo), cwd)
