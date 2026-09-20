"""Runtime configuration and on-disk locations."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _home() -> Path:
    override = os.environ.get("NIMROD_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".nimrod"


@dataclass(frozen=True)
class Config:
    home: Path = field(default_factory=_home)
    claude_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")
        )
    )
    codex_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("CODEX_HOME", Path.home() / ".codex")
        )
    )
    opencode_data: Path = field(
        default_factory=lambda: Path(
            os.environ.get(
                "OPENCODE_DATA",
                Path.home() / ".local" / "share" / "opencode",
            )
        )
    )
    pi_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent")
        )
    )
    embedding_model: str = field(
        default_factory=lambda: os.environ.get(
            "NIMROD_EMBED_MODEL",
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        )
    )
    semantic_enabled: bool = field(
        default_factory=lambda: os.environ.get("NIMROD_NO_EMBEDDINGS") != "1"
    )

    @property
    def pi_sessions(self) -> Path:
        override = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
        if override:
            return Path(override).expanduser()
        return self.pi_root / "sessions"

    @property
    def db_path(self) -> Path:
        return self.home / "work.db"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    def ensure(self) -> "Config":
        self.home.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self


DEFAULT_CONFIG = Config()
