"""OpenAI Codex CLI session adapter.

Two data sources are combined:

* ``~/.codex/state_5.sqlite`` (table ``threads``) -- cheap, indexed metadata:
  cwd, title, git branch/sha, model, timestamps, token usage.
* ``~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`` -- the transcript.

Codex writes a *new* rollout file every time a session is resumed, compacted or
continued, and all of those files share one ``session_id``. We therefore group
rollout files by session id and merge their events so the worklog shows one
entry per logical session instead of losing earlier segments.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .. import db
from ..models import (
    ASSISTANT,
    COMMAND,
    ERROR,
    FILE_DELETE,
    FILE_EDIT,
    FILE_WRITE,
    NOTE,
    PROMPT,
    TOOL,
    Event,
    ParsedSession,
)
from .base import clip, fallback_title, first_line, project_name, to_epoch_ms

PATCH_RE = re.compile(r"\*\*\*\s*(?:Update|Add|Delete)\s+File:\s*(.+)")
CMD_RE = re.compile(r"""cmd\s*:\s*(['"])(.*?)\1""", re.DOTALL)
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")

FILE_TOOL_KINDS = {
    "apply_patch": FILE_EDIT,
    "write_file": FILE_WRITE,
    "create_file": FILE_WRITE,
    "delete_file": FILE_DELETE,
}


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _head_meta(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return None
    try:
        entry = json.loads(first)
    except json.JSONDecodeError:
        return None
    if entry.get("type") != "session_meta":
        return None
    payload = entry.get("payload") or {}
    return payload


def parse_rollout(path: Path) -> ParsedSession | None:
    """Parse a single rollout file into a partial session."""
    p = ParsedSession(agent="codex", session_id=path.stem)
    events: list[Event] = []
    ts_values: list[int] = []
    prompts: list[Event] = []
    assistants: list[Event] = []
    tools: list[Event] = []
    ordinal = 0

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
            payload = entry.get("payload") or {}
            ts = to_epoch_ms(entry.get("timestamp"))
            if ts:
                ts_values.append(ts)

            if etype == "session_meta":
                p.session_id = payload.get("session_id") or payload.get("id") or p.session_id
                p.project_path = payload.get("cwd") or p.project_path
                git = payload.get("git") or {}
                if isinstance(git, dict):
                    p.git_branch = p.git_branch or git.get("branch")
                    p.git_sha = p.git_sha or git.get("commit_hash")
                if not p.started_at:
                    p.started_at = to_epoch_ms(payload.get("timestamp")) or ts
                p.raw["session_meta"] = {
                    "originator": payload.get("originator"),
                    "cli_version": payload.get("cli_version"),
                    "model_provider": payload.get("model_provider"),
                }
            elif etype == "event_msg":
                msg_type = payload.get("type")
                if msg_type == "user_message":
                    text = payload.get("message") or ""
                    if text.strip():
                        ordinal += 1
                        prompts.append(Event(kind=PROMPT, ordinal=ordinal, ts=ts,
                                             text=clip(text, 2000)))
                elif msg_type == "agent_message":
                    text = payload.get("message") or ""
                    if text.strip():
                        ordinal += 1
                        assistants.append(Event(kind=ASSISTANT, ordinal=ordinal, ts=ts,
                                                text=clip(text, 4000)))
                elif msg_type == "turn_aborted":
                    ordinal += 1
                    events.append(Event(kind=NOTE, ordinal=ordinal, ts=ts,
                                        text="turn aborted: " + str(payload.get("reason") or "")))
                elif msg_type == "error":
                    ordinal += 1
                    events.append(Event(kind=ERROR, ordinal=ordinal, ts=ts,
                                        text=clip(str(payload.get("message") or payload), 500)))
            elif etype == "response_item":
                item_type = payload.get("type")
                if item_type == "message" and payload.get("role") == "assistant":
                    text = _message_text(payload)
                    if text.strip():
                        ordinal += 1
                        assistants.append(Event(kind=ASSISTANT, ordinal=ordinal, ts=ts,
                                                text=clip(text, 4000)))
                elif item_type in ("function_call", "custom_tool_call"):
                    ev = _tool_event(payload, ts)
                    if ev is not None:
                        ordinal += 1
                        ev.ordinal = ordinal
                        tools.append(ev)

    p.events = events + prompts + assistants + tools
    p.prompt_count = len(prompts)
    p.tool_count = len(tools)
    if ts_values:
        actual_start = min(ts_values)
        actual_end = max(ts_values)
        p.started_at = min(p.started_at, actual_start) if p.started_at else actual_start
        p.ended_at = max(p.ended_at or 0, actual_end)
    return p


def merge_segments(segments: list[ParsedSession], meta: dict | None) -> ParsedSession:
    """Merge rollout segments that belong to the same logical session."""
    base = ParsedSession(agent="codex", session_id=segments[0].session_id)
    if meta:
        base.session_id = meta.get("id") or base.session_id
        base.project_path = meta.get("cwd")
        base.git_branch = meta.get("git_branch")
        base.git_sha = meta.get("git_sha")
        base.model = meta.get("model")
        base.title = meta.get("title") or None
        base.started_at = meta.get("created_at_ms") or None
        base.ended_at = meta.get("updated_at_ms") or None
        base.tokens_input = int(meta.get("tokens_used") or 0)
        base.raw["thread"] = {
            "source": meta.get("source"),
            "originator": meta.get("originator"),
            "cli_version": meta.get("cli_version"),
            "preview": meta.get("preview"),
        }

    for seg in segments:
        if not base.project_path:
            base.project_path = seg.project_path
        base.git_branch = base.git_branch or seg.git_branch
        base.git_sha = base.git_sha or seg.git_sha
        base.model = base.model or seg.model
        starts = [x for x in (base.started_at, seg.started_at) if x]
        ends = [x for x in (base.ended_at, seg.ended_at) if x]
        base.started_at = min(starts) if starts else None
        base.ended_at = max(ends) if ends else None
        if not meta:
            base.tokens_input += seg.tokens_input
        base.raw.setdefault("segments", []).append(seg.raw.get("log") or seg.session_id)
        base.raw.update({k: v for k, v in seg.raw.items() if k == "session_meta"})

    merged_events = sorted(
        (e for seg in segments for e in seg.events),
        key=lambda e: (e.ts or 0),
    )
    for i, e in enumerate(merged_events, start=1):
        e.ordinal = i
    base.events = merged_events
    base.prompt_count = sum(1 for e in merged_events if e.kind == PROMPT)
    base.tool_count = sum(
        1 for e in merged_events if e.kind in (TOOL, COMMAND, FILE_EDIT, FILE_WRITE,
                                               FILE_DELETE)
    )
    if not base.title:
        base.title = fallback_title(base)
    base.project_name = project_name(base.project_path)
    return base


def _message_text(payload: dict) -> str:
    content = payload.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                out.append(block.get("text") or block.get("input_text") or
                           block.get("output_text") or "")
        return "\n".join(x for x in out if x)
    return ""


def _clean_path(value: str) -> str:
    for sep in ("\\n", "\n", "@@", "\t", "\\t"):
        value = value.split(sep)[0]
    return value.strip().strip('",')


def _tool_event(payload: dict, ts: int | None) -> Event | None:
    name = payload.get("name") or payload.get("type") or "tool"
    raw_input = payload.get("input")
    if raw_input is None:
        raw_input = payload.get("arguments")
    text_input = raw_input if isinstance(raw_input, str) else json.dumps(raw_input or {})

    paths = [_clean_path(x) for x in PATCH_RE.findall(text_input)]
    paths = [x for x in paths if x]
    if paths:
        return Event(kind=FILE_EDIT, ts=ts, target=paths[0].strip(),
                     text=clip(text_input, 2000), meta={"tool": name, "paths": paths})

    cmd_match = CMD_RE.search(text_input)
    if name in ("exec", "shell", "container.exec", "exec_command") or cmd_match:
        cmd = cmd_match.group(2) if cmd_match else first_line(text_input, 300)
        return Event(kind=COMMAND, ts=ts, target=clip(cmd, 1000), text=clip(cmd, 1000),
                     meta={"tool": name})

    if name in FILE_TOOL_KINDS:
        return Event(kind=FILE_TOOL_KINDS[name], ts=ts,
                     text=clip(text_input, 500), meta={"tool": name})

    return Event(kind=TOOL, ts=ts, text=clip(f"{name}: {text_input}", 400),
                 meta={"tool": name})


class CodexSource:
    name = "codex"

    def __init__(self, config):
        self.config = config

    def iter_sessions(self, conn, force: bool = False) -> Iterator[ParsedSession]:
        sessions_root = self.config.codex_root / "sessions"
        if not sessions_root.is_dir():
            return

        groups: dict[str, list[Path]] = {}
        for path in sessions_root.rglob("rollout-*.jsonl"):
            head = _head_meta(path)
            sid = (head or {}).get("session_id") or (head or {}).get("id")
            if not sid:
                m = UUID_RE.search(path.name)
                sid = m.group(1) if m else path.stem
            groups.setdefault(sid, []).append(path)

        threads = self._threads()

        for sid, paths in groups.items():
            paths.sort(key=lambda x: x.name)
            stats = {}
            for path in paths:
                try:
                    st = path.stat()
                except OSError:
                    continue
                stats[path] = st
            if not stats:
                continue
            changed = force or any(
                not db.file_unchanged(conn, str(path), st.st_mtime, st.st_size)
                for path, st in stats.items()
            )
            if not changed:
                continue
            segments = [s for s in (parse_rollout(p) for p in paths) if s is not None]
            if not segments:
                continue
            parsed = merge_segments(segments, threads.get(sid))
            newest = max(stats.items(), key=lambda kv: kv[1].st_mtime)
            parsed.source_path = str(newest[0])
            parsed.source_mtime = newest[1].st_mtime
            parsed.source_size = newest[1].st_size
            parsed.raw["files"] = [str(p) for p in paths]
            yield parsed
            for path, st in stats.items():
                db.mark_file(conn, str(path), st.st_mtime, st.st_size)

    def _threads(self) -> dict[str, dict]:
        db_conn = _open_ro(self.config.codex_root / "state_5.sqlite")
        if db_conn is None:
            return {}
        out: dict[str, dict] = {}
        try:
            rows = db_conn.execute(
                """
                SELECT id, rollout_path, cwd, title, git_branch, git_sha, model,
                       created_at, created_at_ms, updated_at, updated_at_ms,
                       tokens_used, source, originator, cli_version, preview
                FROM threads
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            db_conn.close()
        for r in rows:
            d = {k: r[k] for k in r.keys()}
            d["created_at_ms"] = d.get("created_at_ms") or (
                (d.get("created_at") or 0) * 1000
            )
            d["updated_at_ms"] = d.get("updated_at_ms") or (
                (d.get("updated_at") or 0) * 1000
            )
            out[d["id"]] = d
        return out
