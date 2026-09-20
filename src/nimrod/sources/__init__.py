"""Source registry."""

from __future__ import annotations

from .base import build_summary
from .claude import ClaudeSource
from .codex import CodexSource
from .opencode import OpenCodeSource
from .pi import PiSource


def all_sources(config):
    return [ClaudeSource(config), CodexSource(config), OpenCodeSource(config),
            PiSource(config)]


__all__ = ["all_sources", "build_summary", "ClaudeSource", "CodexSource",
           "OpenCodeSource", "PiSource"]
