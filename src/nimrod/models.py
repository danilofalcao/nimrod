"""Domain model shared by every source adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Event kinds. Kept small on purpose: the store should stay queryable without
# a taxonomy debate.
PROMPT = "prompt"
ASSISTANT = "assistant"
FILE_EDIT = "file_edit"
FILE_WRITE = "file_write"
FILE_READ = "file_read"
FILE_DELETE = "file_delete"
COMMAND = "command"
TOOL = "tool"
COMMIT = "commit"
ERROR = "error"
NOTE = "note"

FILE_KINDS = {FILE_EDIT, FILE_WRITE, FILE_DELETE}


@dataclass
class Event:
    kind: str
    ordinal: int = 0
    ts: int | None = None
    text: str = ""
    target: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParsedSession:
    agent: str
    session_id: str
    project_path: str | None = None
    project_name: str | None = None
    git_branch: str | None = None
    git_sha: str | None = None
    model: str | None = None
    title: str | None = None
    summary: str | None = None
    started_at: int | None = None
    ended_at: int | None = None
    source_path: str | None = None
    source_mtime: float | None = None
    source_size: int | None = None
    prompt_count: int = 0
    tool_count: int = 0
    additions: int = 0
    deletions: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    cost: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)

    @property
    def id(self) -> str:
        return f"{self.agent}:{self.session_id}"

    @property
    def duration_ms(self) -> int | None:
        if self.started_at and self.ended_at and self.ended_at >= self.started_at:
            return int(self.ended_at - self.started_at)
        return None

    @property
    def file_count(self) -> int:
        return len({e.target for e in self.events if e.kind in FILE_KINDS and e.target})
