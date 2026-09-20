"""Ingestion engine: pull sessions from every source into the worklog.

Marking source files as seen and advancing per-source cursors lives here, so
adapters only worry about parsing. Embeddings are computed in batches after the
write transaction commits, keeping the database lock short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from . import db
from .config import Config
from .embeddings import Embedder
from .models import ParsedSession
from .sources import all_sources, build_summary
from .store import Store

EMBED_BATCH = 32


@dataclass
class IngestReport:
    scanned: int = 0
    changed: int = 0
    embedded: int = 0
    per_agent: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "changed": self.changed,
            "embedded": self.embedded,
            "per_agent": self.per_agent,
        }


def ingest(
    config: Config,
    force: bool = False,
    agents: list[str] | None = None,
    project: str | None = None,
    semantic: bool | None = None,
    progress: Callable[[str], None] | None = None,
) -> IngestReport:
    config.ensure()
    conn = db.connect(config.db_path)
    embedder = Embedder(
        model_name=config.embedding_model,
        enabled=config.semantic_enabled if semantic is None else semantic,
        cache_dir=str(config.cache_dir),
    )
    store = Store(conn, embedder)
    report = IngestReport()
    changed_ids: list[str] = []

    try:
        for source in all_sources(config):
            if agents and source.name not in agents:
                continue
            count = 0
            for parsed in source.iter_sessions(conn, force=force):
                if parsed is None:
                    continue
                report.scanned += 1
                if parsed.project_path and project and not _under(parsed.project_path, project):
                    _mark(conn, parsed)
                    continue
                if not parsed.summary:
                    parsed.summary = build_summary(parsed)
                written = store.write_session(parsed, force=force)
                _mark(conn, parsed)
                if written:
                    report.changed += 1
                    count += 1
                    changed_ids.append(parsed.id)
            report.per_agent[source.name] = count
            if progress:
                progress(f"{source.name}: {count} updated")

        for i in range(0, len(changed_ids), EMBED_BATCH):
            batch = changed_ids[i : i + EMBED_BATCH]
            report.embedded += store.embed_sessions(batch)
        if embedder.enabled:
            store.prune_embeddings()
    finally:
        conn.close()
    return report


def _mark(conn, parsed: ParsedSession) -> None:
    if parsed.source_path and parsed.source_mtime is not None and parsed.source_size is not None:
        db.mark_file(conn, parsed.source_path, parsed.source_mtime, parsed.source_size)


def _under(path: str | None, project: str) -> bool:
    if not path:
        return False
    p = project.rstrip("/")
    return path == p or path.startswith(p + "/")
