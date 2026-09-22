"""MCP server exposing the worklog to any MCP-capable agent.

The server is read-first: recall is the point. ``work_record`` exists for the
rare case an agent wants to leave an explicit note, but capture of real work is
handled by ingestion, not by the model.

Run with ``nimrod serve`` or the ``nimrod-mcp`` entry point (stdio).
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any

from . import db
from .config import Config
from .embeddings import Embedder
from .engine import ingest as run_ingest
from .projects import resolve_project
from .store import Store

_CONFIG = Config()


def _open() -> tuple[Any, Store]:
    _CONFIG.ensure()
    conn = db.connect(_CONFIG.db_path)
    embedder = Embedder(
        model_name=_CONFIG.embedding_model,
        enabled=_CONFIG.semantic_enabled,
        cache_dir=str(_CONFIG.cache_dir),
    )
    return conn, Store(conn, embedder)


def _scope(project: str | None, cwd_default: bool = True) -> str | None:
    """Resolve the project scope for a tool call.

    ``project`` accepts a path, a project folder name, or ``"*"``/``"all"`` for
    every project. When omitted, session-oriented tools default to the caller's
    working directory; ``work_search`` defaults to all projects because a query
    often targets a different repo than the one the agent runs in.
    """
    if project and project.strip() in ("*", "all", "global"):
        return None
    if not project:
        if not cwd_default:
            return None
        project = os.environ.get("NIMROD_PROJECT") or os.getcwd()
    # Canonicalize to the enclosing repository so a session started in a
    # subdirectory, or in ``$HOME``, does not resolve to a parent scope.
    return resolve_project(project)


def _to_ms(value: Any, *, end_of_day: bool = False) -> int | None:
    """Coerce a window bound to epoch milliseconds.

    Accepts epoch seconds/milliseconds, or an ISO date/datetime. A naive value
    (``2026-09-21``) is read on the caller's local clock, which is what a user
    means by "yesterday"; ``end_of_day`` extends a date-only ``until`` so the
    named day is included rather than excluded at its first instant.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value if value >= 1e11 else value * 1000)
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        if end_of_day and len(text) == 10:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
        dt = dt.astimezone()
    return int(dt.timestamp() * 1000)


def create_server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(
        "nimrod",
        instructions=(
            "Nimrod is the user's cross-session work memory, shared by Claude "
            "Code, Codex, OpenCode and Pi. The worklog is not injected "
            "automatically: consult these tools yourself whenever you lack "
            "the context to answer -- when the user refers to earlier work, "
            "asks how something was done before, or names another project. "
            "Do not call them on a greeting or preemptively; reach for them "
            "when they would actually supply an answer you do not have. The "
            "'project' argument accepts a path, a project folder name, or "
            "'*' for every project; use it when the user asks about a "
            "different repo than the one you run in. Prefer these tools over "
            "querying the database or CLI yourself."
        ),
    )

    @mcp.tool()
    def work_context(project: str | None = None, limit: int = 6) -> str:
        """Compact brief of the most recent work in a project.

        Returns the latest intents, files touched and outcomes across all
        agents. Call it before guessing about the current project's recent
        history, or when the user asks for it or names another project.
        ``project`` may be a path or a folder name; omit it for the current
        project.
        """
        conn, store = _open()
        try:
            return store.context(_scope(project), limit=limit) or \
                "No recorded work for this project yet."
        finally:
            conn.close()

    @mcp.tool()
    def work_recent(project: str | None = None, agent: str | None = None,
                    limit: int = 10, since: str | None = None,
                    until: str | None = None, detail: bool = False) -> dict:
        """Most recent sessions, newest first.

        ``agent`` filters claude/codex/opencode/pi. ``project`` accepts a path or a
        folder name, or '*' for all projects (default: current project).
        ``since``/``until`` bound the window and accept an ISO date/datetime
        (read on the local clock) or epoch seconds/ms; ``until`` includes the
        whole named day. These compose, so "everything yesterday across all
        projects" is ``project='*', since='2026-09-21', until='2026-09-21'``.
        Rows are brief by default; ``detail=true`` adds summaries.
        """
        conn, store = _open()
        try:
            results = store.recent(
                _scope(project), agent=agent, limit=limit,
                since=_to_ms(since), until=_to_ms(until, end_of_day=True),
                full=detail,
            )
            return {"count": len(results), "results": results}
        finally:
            conn.close()

    @mcp.tool()
    def work_search(query: str, project: str | None = None, agent: str | None = None,
                    since_days: int | None = None, limit: int = 8) -> dict:
        """Hybrid (lexical + semantic) search over past work.

        Searches prompts, summaries, file paths, commands and assistant
        outcomes. Returns sessions ranked by relevance, not by date. Call it
        when you lack the context to answer and past work may hold it.
        Searches every project by default; pass ``project`` (path or folder
        name) to narrow it.
        """
        import time

        since = int((time.time() - since_days * 86400) * 1000) if since_days else None
        conn, store = _open()
        try:
            results = store.search(query, project=_scope(project, cwd_default=False),
                                   agent=agent, since=since, limit=limit)
            return {"query": query, "count": len(results), "results": results}
        finally:
            conn.close()

    @mcp.tool()
    def work_timeline(project: str | None = None, limit: int = 25,
                      since: str | None = None, until: str | None = None,
                      detail: bool = False) -> dict:
        """Chronological list of sessions in a project (oldest to newest).

        ``project`` accepts a path or a folder name; omit it for the current
        project, or pass '*' for all. ``since``/``until`` bound the window (ISO
        date/datetime on the local clock, or epoch seconds/ms). Rows are brief
        by default; ``detail=true`` adds summaries.
        """
        conn, store = _open()
        try:
            results = store.timeline(
                _scope(project), limit=limit,
                since=_to_ms(since), until=_to_ms(until, end_of_day=True),
                full=detail,
            )
            return {"count": len(results), "results": results}
        finally:
            conn.close()

    @mcp.tool()
    def work_session(id: str) -> dict:
        """Full detail of one session, including its normalized events.

        ``id`` is the value in the ``id`` field returned by ``work_recent`` /
        ``work_search`` / ``work_timeline`` (e.g. ``opencode:ses_...``). The raw
        ``session_id`` is also accepted.
        """
        conn, store = _open()
        try:
            data = store.get_session(id)
            return data if data is not None else {"error": "not found", "id": id}
        finally:
            conn.close()

    @mcp.tool()
    def work_ingest(force: bool = False) -> dict:
        """Re-scan the local agent stores and update the worklog.

        Normally unnecessary: ingestion also runs from the CLI and session hooks.
        """
        report = run_ingest(_CONFIG, force=force)
        return report.as_dict()

    @mcp.tool()
    def work_stats() -> dict:
        """Counts and coverage of the worklog database."""
        conn, store = _open()
        try:
            return store.stats()
        finally:
            conn.close()

    @mcp.tool()
    def work_record(note: str, project: str | None = None, title: str | None = None) -> dict:
        """Manually record a work note (optional; capture is automatic)."""
        conn, store = _open()
        try:
            sid = store.record_note(note, project=_scope(project), title=title)
            return {"id": sid}
        finally:
            conn.close()

    return mcp


def main() -> None:
    create_server().run()


if __name__ == "__main__":
    main()
