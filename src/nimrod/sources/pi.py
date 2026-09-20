"""Pi coding agent session adapter.

Pi stores one JSONL file per session under
``<PI_CODING_AGENT_DIR>/sessions/--<slug>--/<timestamp>_<session-id>.jsonl``.
The first line is a ``session`` header with the id and working directory; every
following line is an entry (``message``, ``model_change``, ``compaction``,
``session_info``, ...). Message entries follow pi's ``AgentMessage`` shape:
``user`` / ``assistant`` / ``toolResult`` plus the extended ``bashExecution``,
``compactionSummary`` and ``branchSummary`` roles.

Sessions form a tree and pi may keep abandoned branches in the same file. We
ingest every entry in file (append) order, which gives a faithful timeline of
what happened in the session -- the same "one logical session" treatment the
Codex adapter applies to its rollout segments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from .. import db
from ..models import (
    ASSISTANT,
    COMMAND,
    ERROR,
    FILE_DELETE,
    FILE_EDIT,
    FILE_READ,
    FILE_WRITE,
    NOTE,
    PROMPT,
    TOOL,
    Event,
    ParsedSession,
)
from .base import clip, fallback_title, project_name, to_epoch_ms

READ_TOOLS = {"read"}
WRITE_TOOLS = {"write"}
EDIT_TOOLS = {"edit"}
DELETE_TOOLS = {"delete", "remove"}
COMMAND_TOOLS = {"bash", "powershell"}


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text") or "")
        return "\n".join(x for x in out if x)
    return ""


def _tool_event(name: str, args: dict, ts: int | None) -> Event:
    args = args if isinstance(args, dict) else {}
    target = args.get("path") or args.get("file_path") or args.get("filePath")
    if name in READ_TOOLS:
        return Event(kind=FILE_READ, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in WRITE_TOOLS:
        return Event(kind=FILE_WRITE, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in EDIT_TOOLS:
        return Event(kind=FILE_EDIT, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in DELETE_TOOLS:
        return Event(kind=FILE_DELETE, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in COMMAND_TOOLS:
        cmd = args.get("command") or ""
        return Event(kind=COMMAND, ts=ts, target=clip(cmd, 1000), text=clip(cmd, 1000),
                     meta={"tool": name})

    detail = (
        args.get("description")
        or args.get("pattern")
        or args.get("query")
        or args.get("tool")
        or args.get("url")
        or args.get("prompt")
    )
    if isinstance(detail, str):
        text = f"{name}: {clip(detail, 200)}"
    elif name in ("mcp", "mcpScript") and detail is not None:
        text = f"{name}: {clip(str(detail), 200)}"
    else:
        text = name
    return Event(kind=TOOL, ts=ts, target=target, text=clip(text, 400),
                 meta={"tool": name})


def _add_usage(p: ParsedSession, message: dict) -> None:
    usage = message.get("usage") or {}
    p.tokens_input += int(usage.get("input") or 0)
    p.tokens_input += int(usage.get("cacheRead") or 0)
    p.tokens_input += int(usage.get("cacheWrite") or 0)
    p.tokens_output += int(usage.get("output") or 0)
    cost = usage.get("cost") or {}
    if isinstance(cost, dict):
        p.cost += float(cost.get("total") or 0)


def _assistant_events(message: dict, ts: int | None) -> list[Event]:
    events: list[Event] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and (block.get("text") or "").strip():
            events.append(Event(kind=ASSISTANT, ts=ts, text=clip(block.get("text"), 4000)))
        elif btype == "toolCall":
            events.append(_tool_event(block.get("name") or "tool",
                                      block.get("arguments") or {}, ts))
    return events


def _parse_header(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return {}
    try:
        entry = json.loads(first)
    except json.JSONDecodeError:
        return {}
    return entry if entry.get("type") == "session" else {}


def parse_file(path: Path) -> ParsedSession | None:
    header = _parse_header(path)
    session_id = header.get("id") or path.stem.split("_")[-1]
    p = ParsedSession(agent="pi", session_id=session_id)
    p.project_path = header.get("cwd")
    p.raw["pi"] = {"version": header.get("version"),
                   "parent_session": header.get("parentSession")}

    events: list[Event] = []
    ts_values: list[int] = []
    ordinal = 0
    prompt_count = 0
    tool_count = 0
    title = None
    model = None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return None

    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = entry.get("type")
            entry_ts = to_epoch_ms(entry.get("timestamp"))

            if etype == "session":
                continue
            if etype == "model_change":
                model = entry.get("modelId") or model
                continue
            if etype == "session_info":
                title = entry.get("name") or title
                continue
            if etype == "compaction":
                ordinal += 1
                events.append(Event(kind=NOTE, ordinal=ordinal, ts=entry_ts,
                                    text="context compacted"))
                continue
            if etype == "branch_summary":
                ordinal += 1
                events.append(Event(kind=NOTE, ordinal=ordinal, ts=entry_ts,
                                    text="branch switched"))
                continue
            if etype != "message":
                continue

            message = entry.get("message") or {}
            role = message.get("role")
            ts = to_epoch_ms(message.get("timestamp")) or entry_ts
            if ts:
                ts_values.append(ts)

            if role == "user":
                text = _content_text(message.get("content"))
                if text.strip():
                    ordinal += 1
                    prompt_count += 1
                    events.append(Event(kind=PROMPT, ordinal=ordinal, ts=ts,
                                        text=clip(text, 2000)))
            elif role == "assistant":
                if message.get("model"):
                    model = message["model"]
                _add_usage(p, message)
                for ev in _assistant_events(message, ts):
                    ordinal += 1
                    ev.ordinal = ordinal
                    if ev.kind in (TOOL, COMMAND, FILE_EDIT, FILE_WRITE, FILE_READ,
                                   FILE_DELETE):
                        tool_count += 1
                    events.append(ev)
            elif role == "toolResult":
                if message.get("isError"):
                    text = _content_text(message.get("content")) or message.get("toolName")
                    ordinal += 1
                    events.append(Event(kind=ERROR, ordinal=ordinal, ts=ts,
                                        text=clip(text, 500),
                                        meta={"tool": message.get("toolName")}))
            elif role == "bashExecution":
                command = message.get("command") or ""
                if command.strip():
                    ordinal += 1
                    tool_count += 1
                    events.append(Event(kind=COMMAND, ordinal=ordinal, ts=ts,
                                        target=clip(command, 1000), text=clip(command, 1000),
                                        meta={"tool": "bash", "user": True}))
            elif role in ("compactionSummary", "branchSummary"):
                ordinal += 1
                events.append(Event(kind=NOTE, ordinal=ordinal, ts=ts,
                                    text=clip(message.get("summary") or role, 1000)))

    if not events:
        return None

    p.model = model
    p.title = title or fallback_title(ParsedSession(agent="pi", session_id=p.session_id,
                                                    events=events))
    p.prompt_count = prompt_count
    p.tool_count = tool_count
    p.started_at = min(ts_values) if ts_values else to_epoch_ms(header.get("timestamp"))
    p.ended_at = max(ts_values) if ts_values else p.started_at
    p.project_name = project_name(p.project_path)
    p.events = events
    p.raw["log"] = str(path)
    return p


class PiSource:
    name = "pi"

    def __init__(self, config):
        self.config = config

    def iter_files(self) -> Iterator[Path]:
        root = self.config.pi_sessions
        if not root.is_dir():
            return
        for path in root.rglob("*.jsonl"):
            if path.is_file():
                yield path

    def iter_sessions(self, conn, force: bool = False) -> Iterator[ParsedSession]:
        for path in self.iter_files():
            try:
                stat = path.stat()
            except OSError:
                continue
            if not force and db.file_unchanged(conn, str(path), stat.st_mtime, stat.st_size):
                continue
            parsed = parse_file(path)
            if parsed is None:
                continue
            parsed.source_path = str(path)
            parsed.source_mtime = stat.st_mtime
            parsed.source_size = stat.st_size
            yield parsed
