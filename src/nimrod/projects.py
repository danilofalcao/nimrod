"""Project identity: which repository does a working directory belong to?

Agents are routinely launched from directories that are *not* projects -- the
home directory, ``/tmp``, or a folder that merely contains repositories. If
those directories are treated as projects, then:

* a brief for one repository lists work from every repository nested under it
  (because a parent path is a prefix of all of them), and
* auto-recall matches on generic name tokens such as ``home`` or ``tmp``.

The helpers here answer two separate questions:

* :func:`find_project_root` -- which repository encloses this directory?
* :func:`project_name` -- a short, separator-free label for a project path.

``project_path`` values are canonicalized to the repository root on ingestion
(:func:`resolve_project`), which lets every query match *exactly* instead of by
path prefix.
"""

from __future__ import annotations

import os
from pathlib import Path

# Version-control markers identify the repository itself.
VCS_MARKERS = (".git", ".hg", ".svn")

# Package/build manifests identify a standalone project without a VCS checkout.
MANIFEST_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "package.json",
    "Cargo.toml",
    "go.mod",
    "composer.json",
    "Gemfile",
    "pom.xml",
    "build.gradle",
    "CMakeLists.txt",
)

# Directories that hold projects but are not projects themselves.
CONTAINER_PATHS = frozenset({
    "/",
    "/home",
    "/tmp",
    "/var/tmp",
    "/root",
    "/usr",
    "/etc",
    "/opt",
    "/var",
    "/srv",
    "/mnt",
    "/media",
    "/dev",
    "/proc",
    "/sys",
    "/run",
})


def normalize(path: str | None) -> str | None:
    """Expand ``~`` and collapse redundant separators; ``None`` stays ``None``."""
    if not path:
        return None
    return os.path.normpath(os.path.expanduser(path))


def is_container_path(path: str | None) -> bool:
    """True for paths that contain projects but are not projects themselves."""
    p = normalize(path)
    if not p:
        return False
    if p in CONTAINER_PATHS:
        return True
    home = normalize(str(Path.home()))
    return p == home or (home is not None and p == os.path.dirname(home))


def in_dot_directory(path: str | None) -> bool:
    """True when any path component is hidden (config/state, not source)."""
    p = normalize(path)
    if not p:
        return False
    return any(part.startswith(".") for part in Path(p).parts if part not in ("/",))


def find_project_root(path: str | None) -> str | None:
    """Nearest enclosing repository root, or ``None`` when there is none.

    VCS markers are searched first so that a package manifest inside a
    monorepo (``repo/pkg/package.json``) does not shadow the repository that
    contains it. Relative paths are never resolved: they are project *names*,
    not directories.
    """
    p = normalize(path)
    if not p or not os.path.isabs(p):
        return None
    for markers in (VCS_MARKERS, MANIFEST_MARKERS):
        node = Path(p)
        while True:
            if any((node / marker).exists() for marker in markers):
                return str(node)
            parent = node.parent
            if parent == node:
                break
            node = parent
    return None


def resolve_project(path: str | None) -> str | None:
    """Canonical project path for a session's working directory."""
    p = normalize(path)
    if not p:
        return None
    return find_project_root(p) or p


def project_name(path: str | None) -> str | None:
    """Short label for a project path; never contains a path separator.

    Returning a full path here used to leak into token matching, where
    ``/home/user`` became the tokens ``home`` and ``user`` and matched almost
    any prompt that mentioned an absolute path.
    """
    p = normalize(path)
    if not p:
        return None
    name = Path(p).name
    if not name:
        return "root"
    if p == normalize(str(Path.home())):
        return "home"
    return name


def is_auto_recall_candidate(path: str | None, cwd: str | None) -> bool:
    """Whether ``path`` is a project worth injecting into a prompt.

    Only real repositories qualify. Container directories, hidden directories
    and any ancestor of the caller's own working directory are rejected: the
    agent is already there and received that brief at session start.
    """
    root = find_project_root(path)
    if root is None:
        return False
    if is_container_path(root) or in_dot_directory(root):
        return False
    if cwd:
        c = normalize(cwd)
        if c and (c == root or c.startswith(root + os.sep)):
            return False
    return True
