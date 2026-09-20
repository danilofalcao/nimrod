"""Claude Code session adapter.

Reads the JSONL transcripts under ``~/.claude/projects/<slug>/<session>.jsonl``.
Each line is an event; we keep only what describes work: human prompts,
assistant messages, tool calls (file edits / commands) and the session
metadata embedded in the entries.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from .. import db
from ..models import (
    ASSISTANT,
    COMMAND,
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
from .base import clip, fallback_title, first_line, project_name, to_epoch_ms

READ_TOOLS = {"Read", "NotebookRead"}
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit", "Update"}
WRITE_TOOLS = {"Write", "NotebookWrite", "create_file"}
DELETE_TOOLS = {"Delete", "Remove"}
COMMAND_TOOLS = {"Bash", "BashOutput", "KillShell", "run_terminal_cmd"}


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                out.append(block.get("text") or "")
        return "\n".join(x for x in out if x)
    return ""


def _is_human(entry: dict) -> bool:
    origin = entry.get("origin") or {}
    if isinstance(origin, dict) and origin.get("kind") == "human":
        return True
    if entry.get("promptSource") == "typed":
        return True
    if entry.get("userType") == "external":
        return True
    return False


def parse_file(path: Path) -> ParsedSession | None:
    session_id = path.stem
    p = ParsedSession(agent="claude", session_id=session_id)
    events: list[Event] = []
    ts_values: list[int] = []
    title = None
    model = None
    ordinal = 0
    prompt_count = 0
    tool_count = 0

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
            sid = entry.get("sessionId")
            if sid and not p.session_id:
                p.session_id = sid
            if entry.get("cwd"):
                p.project_path = entry["cwd"]
            if entry.get("gitBranch"):
                p.git_branch = entry["gitBranch"]
            ts = to_epoch_ms(entry.get("timestamp"))
            if ts:
                ts_values.append(ts)

            if etype == "ai-title":
                title = entry.get("aiTitle") or entry.get("title") or title
            elif etype == "user":
                if entry.get("isMeta") or _is_tool_result(entry):
                    continue
                if not _is_human(entry) and not _looks_like_prompt(entry):
                    continue
                text = _content_text((entry.get("message") or {}).get("content"))
                if text.strip():
                    ordinal += 1
                    prompt_count += 1
                    events.append(Event(kind=PROMPT, ordinal=ordinal, ts=ts,
                                        text=clip(text, 2000)))
            elif etype == "assistant":
                message = entry.get("message") or {}
                if message.get("model"):
                    model = message["model"]
                usage = message.get("usage") or {}
                p.tokens_input += int(usage.get("input_tokens") or 0)
                p.tokens_input += int(usage.get("cache_read_input_tokens") or 0)
                p.tokens_input += int(usage.get("cache_creation_input_tokens") or 0)
                p.tokens_output += int(usage.get("output_tokens") or 0)
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and (block.get("text") or "").strip():
                        ordinal += 1
                        events.append(Event(kind=ASSISTANT, ordinal=ordinal, ts=ts,
                                            text=clip(block.get("text"), 4000)))
                    elif btype == "tool_use":
                        ev = _tool_event(block, ts, ordinal + 1)
                        if ev is not None:
                            ordinal += 1
                            tool_count += 1
                            ev.ordinal = ordinal
                            events.append(ev)
            elif etype == "system" and entry.get("subtype") == "compact_boundary":
                ordinal += 1
                events.append(Event(kind=NOTE, ordinal=ordinal, ts=ts,
                                    text="context compacted"))

    if not events and not ts_values:
        return None
    p.model = model
    p.title = title or fallback_title(ParsedSession(agent="claude", session_id=p.session_id,
                                                     events=events))
    p.prompt_count = prompt_count
    p.tool_count = tool_count
    p.started_at = min(ts_values) if ts_values else None
    p.ended_at = max(ts_values) if ts_values else None
    p.project_name = project_name(p.project_path)
    p.events = events
    p.raw = {"log": str(path)}
    return p


def _looks_like_prompt(entry: dict) -> bool:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return not any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _is_tool_result(entry: dict) -> bool:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
    return False


def _tool_event(block: dict, ts: int | None, ordinal: int) -> Event | None:
    name = block.get("name") or ""
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    target = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if name in READ_TOOLS:
        return Event(kind=FILE_READ, ts=ts, target=target, text=target or "")
    if name in EDIT_TOOLS:
        return Event(kind=FILE_EDIT, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in WRITE_TOOLS:
        return Event(kind=FILE_WRITE, ts=ts, target=target, text=target or "",
                     meta={"tool": name})
    if name in DELETE_TOOLS:
        return Event(kind=FILE_DELETE, ts=ts, target=target, text=target or "")
    if name in COMMAND_TOOLS:
        cmd = inp.get("command") or ""
        return Event(kind=COMMAND, ts=ts, target=clip(cmd, 1000), text=clip(cmd, 1000))
    text = name
    if inp.get("description"):
        text = f"{name}: {inp['description']}"
    elif inp.get("pattern"):
        text = f"{name}: {inp['pattern']}"
    elif inp.get("query"):
        text = f"{name}: {clip(str(inp['query']), 200)}"
    elif inp.get("url"):
        text = f"{name}: {inp['url']}"
    return Event(kind=TOOL, ts=ts, target=target, text=clip(text, 400),
                 meta={"tool": name})


class ClaudeSource:
    name = "claude"

    def __init__(self, config):
        self.config = config

    def iter_files(self) -> Iterator[Path]:
        root = self.config.claude_root / "projects"
        if not root.is_dir():
            return
        for path in root.glob("*/*.jsonl"):
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
