"""OpenCode session adapter.

OpenCode stores everything in ``~/.local/share/opencode/opencode.db``:
``session`` (metadata), ``message`` (role) and ``part`` (text, reasoning,
tool calls). We read it read-only so a running OpenCode is never disturbed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .. import db
from ..models import (
    ASSISTANT,
    COMMAND,
    FILE_EDIT,
    FILE_DELETE,
    FILE_READ,
    FILE_WRITE,
    PROMPT,
    TOOL,
    Event,
    ParsedSession,
)
from .base import clip, project_name

FILE_EDIT_TOOLS = {"edit", "patch", "str_replace", "apply_patch"}
FILE_WRITE_TOOLS = {"write"}
FILE_READ_TOOLS = {"read"}
FILE_DELETE_TOOLS = {"delete", "remove"}
COMMAND_TOOLS = {"bash", "shell", "exec_command"}


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _j(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _tool_event(data: dict, ts: int | None) -> Event | None:
    tool = (data.get("tool") or "").lower()
    state = data.get("state") or {}
    inp = state.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    target = inp.get("filePath") or inp.get("path") or inp.get("file_path")
    if tool in FILE_EDIT_TOOLS:
        return Event(kind=FILE_EDIT, ts=ts, target=target, text=target or "",
                     meta={"tool": tool})
    if tool in FILE_WRITE_TOOLS:
        return Event(kind=FILE_WRITE, ts=ts, target=target, text=target or "",
                     meta={"tool": tool})
    if tool in FILE_READ_TOOLS:
        return Event(kind=FILE_READ, ts=ts, target=target, text=target or "")
    if tool in FILE_DELETE_TOOLS:
        return Event(kind=FILE_DELETE, ts=ts, target=target, text=target or "")
    if tool in COMMAND_TOOLS:
        cmd = inp.get("command") or ""
        return Event(kind=COMMAND, ts=ts, target=clip(cmd, 1000), text=clip(cmd, 1000))
    desc = inp.get("description") or inp.get("query") or inp.get("pattern") or ""
    text = f"{tool}: {desc}" if desc else tool
    return Event(kind=TOOL, ts=ts, target=target, text=clip(text, 400),
                 meta={"tool": tool})


def parse_session(db_conn: sqlite3.Connection, row: sqlite3.Row) -> ParsedSession:
    sid = row["id"]
    p = ParsedSession(agent="opencode", session_id=sid)
    p.project_path = row["directory"]
    p.project_name = project_name(row["directory"])
    p.git_branch = None
    p.title = row["title"] or None
    model = row["model"]
    if model:
        parsed_model = _j(model)
        if isinstance(parsed_model, dict):
            p.model = parsed_model.get("modelID") or parsed_model.get("id") or str(model)
        else:
            p.model = str(model)
    p.started_at = row["time_created"]
    p.ended_at = row["time_updated"]
    p.additions = int(row["summary_additions"] or 0)
    p.deletions = int(row["summary_deletions"] or 0)
    p.tokens_input = int(row["tokens_input"] or 0)
    p.tokens_output = int(row["tokens_output"] or 0)
    p.cost = float(row["cost"] or 0)
    p.raw["opencode"] = {
        "agent": row["agent"],
        "version": row["version"],
        "summary_files": row["summary_files"],
        "parent_id": row["parent_id"],
    }

    events: list[Event] = []
    ordinal = 0
    messages = db_conn.execute(
        "SELECT id, data, time_created FROM message WHERE session_id=? ORDER BY time_created",
        (sid,),
    ).fetchall()
    for msg in messages:
        mdata = _j(msg["data"]) or {}
        role = mdata.get("role")
        parts = db_conn.execute(
            "SELECT data, time_created FROM part WHERE message_id=? ORDER BY time_created",
            (msg["id"],),
        ).fetchall()
        for part in parts:
            pdata = _j(part["data"]) or {}
            ptype = pdata.get("type")
            ts = part["time_created"] or msg["time_created"]
            if ptype == "text":
                text = pdata.get("text") or ""
                if not text.strip():
                    continue
                ordinal += 1
                if role == "user":
                    events.append(Event(kind=PROMPT, ordinal=ordinal, ts=ts,
                                        text=clip(text, 2000)))
                elif role == "assistant":
                    events.append(Event(kind=ASSISTANT, ordinal=ordinal, ts=ts,
                                        text=clip(text, 4000)))
            elif ptype == "tool":
                ev = _tool_event(pdata, ts)
                if ev is not None:
                    ordinal += 1
                    ev.ordinal = ordinal
                    events.append(ev)
            elif ptype == "patch":
                for path in pdata.get("files") or []:
                    if not path:
                        continue
                    ordinal += 1
                    events.append(Event(kind=FILE_EDIT, ordinal=ordinal, ts=ts,
                                        target=path, text=path,
                                        meta={"tool": "patch"}))

    p.events = events
    p.prompt_count = sum(1 for e in events if e.kind == PROMPT)
    p.tool_count = sum(1 for e in events if e.kind in (TOOL, COMMAND, FILE_EDIT,
                                                       FILE_WRITE, FILE_READ, FILE_DELETE))
    if not p.title:
        for e in events:
            if e.kind == PROMPT and e.text:
                p.title = clip(e.text.splitlines()[0], 90)
                break
    if not p.title:
        p.title = f"opencode session {sid[:12]}"
    return p


class OpenCodeSource:
    name = "opencode"

    def __init__(self, config):
        self.config = config

    def iter_sessions(self, conn, force: bool = False) -> Iterator[ParsedSession]:
        db_conn = _open_ro(self.config.opencode_data / "opencode.db")
        if db_conn is None:
            return
        since = 0 if force else db.get_state(conn, self.name)
        max_updated = since
        try:
            sql = "SELECT * FROM session"
            args: list[Any] = []
            if since:
                sql += " WHERE time_updated > ?"
                args.append(since)
            for row in db_conn.execute(sql, args).fetchall():
                max_updated = max(max_updated, int(row["time_updated"] or 0))
                yield parse_session(db_conn, row)
        except sqlite3.Error:
            return
        finally:
            db_conn.close()
        if max_updated > since:
            db.set_state(conn, self.name, max_updated)
