"""Shared helpers for source adapters."""

from __future__ import annotations

import datetime as _dt
import os
from typing import Any

from ..models import Event, ParsedSession
from ..projects import project_name  # re-exported for source adapters

__all__ = [
    "clip",
    "fallback_title",
    "file_meta",
    "first_line",
    "is_probably_binary_path",
    "project_name",
    "relative_to_project",
    "to_epoch_ms",
    "build_summary",
]


def to_epoch_ms(value: Any) -> int | None:
    """Best-effort conversion of ISO strings / numbers to epoch milliseconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e14:  # already ms
            return int(v)
        if v > 1e11:  # ms with low sign? still ms
            return int(v)
        if v > 1e9:  # seconds
            return int(v * 1000)
        return int(v)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            dt = _dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return int(dt.timestamp() * 1000)
    return None


def clip(text: str | None, limit: int = 4000) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def first_line(text: str, limit: int = 120) -> str:
    line = (text or "").strip().splitlines()
    return clip(line[0], limit) if line else ""


def file_meta(kind: str, path: str, extra: dict[str, Any] | None = None) -> Event:
    return Event(kind=kind, target=path, text=path, meta=extra or {})


def is_probably_binary_path(path: str) -> bool:
    ignored = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".so", ".bin")
    return path.lower().endswith(ignored)


def relative_to_project(path: str | None, project: str | None) -> str | None:
    if not path:
        return None
    if project:
        try:
            return os.path.relpath(path, project)
        except ValueError:
            return path
    return path


def build_summary(p: ParsedSession) -> str:
    """Deterministic, model-free summary of a session.

    Order: intents -> changed files -> commands -> final assistant message.
    """
    from ..models import ASSISTANT, COMMAND, FILE_KINDS, PROMPT

    prompts = [e.text for e in p.events if e.kind == PROMPT and e.text]
    files: list[str] = []
    for e in p.events:
        if e.kind in FILE_KINDS and e.target and e.target not in files:
            files.append(e.target)
    commands = [e.target for e in p.events if e.kind == COMMAND and e.target]
    assistant = [e.text for e in p.events if e.kind == ASSISTANT and e.text]

    parts: list[str] = []
    if prompts:
        intent = " | ".join(clip(x, 200) for x in prompts[:4])
        parts.append(f"Intent: {intent}")
    if files:
        shown = ", ".join(relative_to_project(f, p.project_path) or f for f in files[:12])
        more = "" if len(files) <= 12 else f" (+{len(files) - 12} more)"
        parts.append(f"Changed: {shown}{more}")
    if commands:
        shown = "; ".join(clip(c, 120) for c in commands[:5])
        more = "" if len(commands) <= 5 else f" (+{len(commands) - 5} more)"
        parts.append(f"Ran: {shown}{more}")
    if assistant:
        parts.append(f"Result: {clip(assistant[-1], 700)}")
    return clip("\n".join(parts), 4000)


def fallback_title(p: ParsedSession) -> str:
    from ..models import PROMPT

    for e in p.events:
        if e.kind == PROMPT and e.text:
            return first_line(e.text, 90)
    return f"{p.agent} session {p.session_id[:8]}"
