"""Query/write layer over the worklog database.

Search is hybrid: FTS5/BM25 over session summaries and events, fused via
Reciprocal Rank Fusion with local-embedding cosine similarity when available.
All retrieval can be scoped to a project path and to a time window.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterator, Sequence

from . import db
from .embeddings import Embedder, cosines, pack, unpack
from .models import FILE_KINDS, ParsedSession


def _fts_query(query: str) -> str:
    tokens = [t for t in query.replace('"', " ").split() if t]
    if not tokens:
        return ""
    return " OR ".join('"{}"'.format(t) for t in tokens)


def _project_clause(project: str | None) -> tuple[str, list[Any]]:
    """Match a session by path prefix *or* project name.

    Accepting names matters: an agent often runs in one repo while asking about
    another ("what did I do in wfkit-archinstall?"), so the filter cannot rely on
    the path alone.
    """
    if not project:
        return "", []
    p = project.rstrip("/")
    name = p.rsplit("/", 1)[-1]
    return (
        " AND (s.project_path = ? OR s.project_path LIKE ?"
        " OR s.project_name = ? OR s.project_name LIKE ?)",
        [p, p + "/%", name, "%" + name + "%"],
    )


class Store:
    def __init__(self, conn: sqlite3.Connection, embedder: Embedder | None = None):
        self.conn = conn
        self.embedder = embedder or Embedder()

    # ------------------------------------------------------------------ writes

    @contextmanager
    def _tx(self) -> Iterator[None]:
        self.conn.execute("BEGIN")
        try:
            yield
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def _content_hash(self, p: ParsedSession) -> str:
        h = hashlib.sha256()
        h.update(p.id.encode())
        h.update(str(p.ended_at).encode())
        h.update(str(p.source_size).encode())
        h.update(str(len(p.events)).encode())
        h.update((p.summary or "").encode())
        return h.hexdigest()

    def write_session(self, p: ParsedSession, force: bool = False) -> bool:
        """Insert or replace one session. Returns True if the store changed."""
        digest = self._content_hash(p)
        row = self.conn.execute(
            "SELECT content_hash FROM sessions WHERE id=?", (p.id,)
        ).fetchone()
        if row is not None and row["content_hash"] == digest and not force:
            return False

        with self._tx():
            self.conn.execute("DELETE FROM events WHERE session_id=?", (p.id,))
            self.conn.execute("DELETE FROM sessions_fts WHERE sid=?", (p.id,))
            self.conn.execute("DELETE FROM events_fts WHERE session_id=?", (p.id,))
            self.conn.execute("DELETE FROM sessions WHERE id=?", (p.id,))
            self.conn.execute(
                """
                INSERT INTO sessions(
                    id, agent, session_id, project_path, project_name, git_branch,
                    git_sha, model, title, summary, started_at, ended_at,
                    duration_ms, source_path, source_mtime, source_size,
                    prompt_count, tool_count, file_count, additions, deletions,
                    tokens_input, tokens_output, cost, content_hash, raw, ingested_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    p.id, p.agent, p.session_id, p.project_path, p.project_name,
                    p.git_branch, p.git_sha, p.model, p.title, p.summary,
                    p.started_at, p.ended_at, p.duration_ms, p.source_path,
                    p.source_mtime, p.source_size, p.prompt_count, p.tool_count,
                    p.file_count, p.additions, p.deletions, p.tokens_input,
                    p.tokens_output, p.cost, digest, db.to_json(p.raw), db.now_ms(),
                ),
            )
            self.conn.execute(
                "INSERT INTO sessions_fts(sid, title, summary, project) VALUES(?,?,?,?)",
                (p.id, p.title or "", p.summary or "", p.project_name or ""),
            )
            for e in p.events:
                cur = self.conn.execute(
                    """
                    INSERT INTO events(session_id, ordinal, kind, ts, text, target, meta)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (
                        p.id, e.ordinal, e.kind, e.ts, e.text, e.target,
                        db.to_json(e.meta) if e.meta else None,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO events_fts(rowid, session_id, text, target) VALUES(?,?,?,?)",
                    (cur.lastrowid, p.id, e.text or "", e.target or ""),
                )
        return True

    def embed_sessions(self, session_ids: Sequence[str]) -> int:
        if not self.embedder.enabled or not session_ids:
            return 0
        rows = []
        for sid in session_ids:
            row = self.conn.execute(
                "SELECT id, title, summary, project_name FROM sessions WHERE id=?",
                (sid,),
            ).fetchone()
            if row:
                text = " ".join(
                    x for x in (row["title"], row["summary"], row["project_name"]) if x
                ).strip()
                if text:
                    rows.append((sid, text))
        if not rows:
            return 0
        vectors = self.embedder.embed([t for _, t in rows])
        if vectors is None:
            return 0
        model = self.embedder.model_name
        with self._tx():
            for (sid, _), vec in zip(rows, vectors):
                self.conn.execute(
                    "DELETE FROM embeddings WHERE owner_type='session' AND owner_id=?",
                    (sid,),
                )
                self.conn.execute(
                    """
                    INSERT INTO embeddings(owner_type, owner_id, model, dim, vector)
                    VALUES('session', ?, ?, ?, ?)
                    """,
                    (sid, model, int(vec.shape[0]), pack(vec)),
                )
        return len(rows)

    def prune_embeddings(self) -> int:
        """Drop embeddings produced by a different (e.g. replaced) model."""
        model = self.embedder.model_name
        cur = self.conn.execute("DELETE FROM embeddings WHERE model != ?", (model,))
        return cur.rowcount

    def pending_embedding_ids(self, limit: int = 500) -> list[str]:
        if not self.embedder.enabled:
            return []
        model = self.embedder.model_name
        rows = self.conn.execute(
            """
            SELECT s.id FROM sessions s
            LEFT JOIN embeddings e
              ON e.owner_id = s.id AND e.owner_type = 'session' AND e.model = ?
            WHERE e.owner_id IS NULL
            ORDER BY COALESCE(s.ended_at, s.started_at) DESC
            LIMIT ?
            """,
            (model, limit),
        ).fetchall()
        return [r["id"] for r in rows]

    def delete_missing(self, project: str | None = None) -> int:
        """Remove sessions whose source store no longer exists is overkill; no-op hook."""
        return 0

    def record_note(self, note: str, project: str | None = None,
                    title: str | None = None) -> str:
        """Store a manually written work note as a synthetic session."""
        import uuid
        from pathlib import Path

        from .models import NOTE, Event, ParsedSession

        sid = uuid.uuid4().hex[:16]
        now = db.now_ms()
        p = ParsedSession(
            agent="note",
            session_id=sid,
            project_path=project,
            project_name=Path(project).name if project else None,
            title=title or (note.strip().splitlines()[0][:90] if note.strip() else "note"),
            summary=note,
            started_at=now,
            ended_at=now,
            prompt_count=1,
        )
        p.events = [Event(kind=NOTE, ordinal=1, ts=now, text=note)]
        self.write_session(p, force=True)
        self.embed_sessions([p.id])
        return p.id

    # ----------------------------------------------------------------- queries

    def _session_rows(self, ids: Sequence[str]) -> dict[str, sqlite3.Row]:
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM sessions WHERE id IN ({placeholders})", list(ids)
        ).fetchall()
        return {r["id"]: r for r in rows}

    def _lexical_sessions(self, query: str, project: str | None, agent: str | None,
                          since: int | None, limit: int) -> list[tuple[str, float]]:
        match = _fts_query(query)
        if not match:
            return []
        clause, params = _project_clause(project)
        sql = f"""
            SELECT sessions_fts.sid AS sid, bm25(sessions_fts) AS score
            FROM sessions_fts JOIN sessions s ON s.id = sessions_fts.sid
            WHERE sessions_fts MATCH ?{clause}
        """
        args: list[Any] = [match, *params]
        if agent:
            sql += " AND s.agent = ?"
            args.append(agent)
        if since:
            sql += " AND COALESCE(s.ended_at, s.started_at, 0) >= ?"
            args.append(since)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [(r["sid"], float(r["score"])) for r in rows]

    def _lexical_events(self, query: str, project: str | None, agent: str | None,
                        since: int | None, limit: int) -> list[tuple[str, float]]:
        match = _fts_query(query)
        if not match:
            return []
        clause, params = _project_clause(project)
        sql = f"""
            SELECT events_fts.session_id AS sid, bm25(events_fts) AS score
            FROM events_fts
            JOIN sessions s ON s.id = events_fts.session_id
            WHERE events_fts MATCH ?{clause}
        """
        args: list[Any] = [match, *params]
        if agent:
            sql += " AND s.agent = ?"
            args.append(agent)
        if since:
            sql += " AND COALESCE(s.ended_at, s.started_at, 0) >= ?"
            args.append(since)
        sql += " ORDER BY score LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        seen: set[str] = set()
        out: list[tuple[str, float]] = []
        for r in rows:
            if r["sid"] in seen:
                continue
            seen.add(r["sid"])
            out.append((r["sid"], float(r["score"])))
        return out

    def _semantic(self, query: str, project: str | None, agent: str | None,
                  since: int | None, limit: int,
                  min_similarity: float = 0.30) -> list[tuple[str, float]]:
        if not self.embedder.enabled:
            return []
        qv = self.embedder.embed([query])
        if qv is None:
            return []
        model = self.embedder.model_name
        rows = self.conn.execute(
            """
            SELECT emb.owner_id AS sid, emb.vector AS blob, emb.dim AS dim
            FROM embeddings emb JOIN sessions s ON s.id = emb.owner_id
            WHERE emb.model = ?
            """,
            (model,),
        ).fetchall()
        if not rows:
            return []
        import numpy as np

        vectors = np.stack([unpack(r["blob"], r["dim"]) for r in rows])
        sims = cosines(qv[0], vectors)
        order = [i for i in sims.argsort()[::-1] if float(sims[i]) >= min_similarity]
        order = order[: limit * 4]
        p = (project or "").rstrip("/")
        name = p.rsplit("/", 1)[-1] if p else ""
        # apply filters via a lookup query
        meta = self._session_rows([rows[i]["sid"] for i in order])
        results: list[tuple[str, float]] = []
        for i in order:
            sid = rows[i]["sid"]
            r = meta.get(sid)
            if r is None:
                continue
            if p and not (
                r["project_path"] == p
                or (r["project_path"] or "").startswith(p + "/")
                or r["project_name"] == name
                or name in (r["project_name"] or "")
            ):
                continue
            if agent and r["agent"] != agent:
                continue
            end = r["ended_at"] or r["started_at"] or 0
            if since and end < since:
                continue
            results.append((sid, float(sims[i])))
            if len(results) >= limit:
                break
        return results

    def project_index(self) -> list[dict[str, Any]]:
        """Distinct projects known to the worklog (name + path + session count)."""
        rows = self.conn.execute(
            """
            SELECT project_name AS name, project_path AS path, count(*) AS sessions,
                   max(COALESCE(ended_at, started_at)) AS last_seen
            FROM sessions
            WHERE project_name IS NOT NULL AND project_name != ''
            GROUP BY project_name, project_path
            """
        ).fetchall()
        return [
            {"name": r["name"], "path": r["path"],
             "sessions": r["sessions"], "last_seen": r["last_seen"]}
            for r in rows
        ]

    def search(self, query: str, project: str | None = None, agent: str | None = None,
               since: int | None = None, limit: int = 10) -> list[dict[str, Any]]:
        rankings: list[tuple[list[tuple[str, float]], float]] = []
        for fn, weight in ((self._lexical_sessions, 1.0), (self._lexical_events, 0.7)):
            ranked = fn(query, project, agent, since, limit * 5)
            rankings.append((ranked, weight))
        sem = self._semantic(query, project, agent, since, limit * 5)
        rankings.append((sem, 0.8))

        k = 60
        scores: dict[str, float] = {}
        for ranked, weight in rankings:
            for rank, (sid, _) in enumerate(ranked):
                scores[sid] = scores.get(sid, 0.0) + weight / (k + rank + 1)
        order = sorted(scores, key=lambda s: scores[s], reverse=True)[:limit]
        meta = self._session_rows(order)
        out = []
        for sid in order:
            r = meta.get(sid)
            if r:
                out.append(self._row_to_dict(r, matched=True))
        return out

    def recent(self, project: str | None = None, agent: str | None = None,
               limit: int = 10) -> list[dict[str, Any]]:
        clause, params = _project_clause(project)
        where = clause.replace(" AND ", " WHERE ", 1) if clause else ""
        args: list[Any] = list(params)
        if agent:
            where += (" AND " if where else " WHERE ") + "s.agent = ?"
            args.append(agent)
        sql = f"SELECT s.* FROM sessions s{where} ORDER BY COALESCE(s.ended_at, s.started_at) DESC LIMIT ?"
        args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def timeline(self, project: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        return list(reversed(self.recent(project=project, limit=limit)))

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        data = self._row_to_dict(row)
        events = self.conn.execute(
            "SELECT kind, ts, text, target, meta FROM events WHERE session_id=? ORDER BY ordinal",
            (row["id"],),
        ).fetchall()
        data["events"] = [
            {
                "kind": e["kind"],
                "ts": e["ts"],
                "text": e["text"],
                "target": e["target"],
            }
            for e in events
        ]
        return data

    def context(self, project: str | None, limit: int = 5) -> str:
        sessions = self.recent(project=project, limit=limit)
        if not sessions:
            return ""
        name = None
        for s in sessions:
            if s.get("project_name"):
                name = s["project_name"]
                break
        lines = [f"Nimrod worklog for {name or project or 'all projects'} (newest first):"]
        files: dict[str, int] = {}
        for s in sessions:
            when = time.strftime(
                "%Y-%m-%d %H:%M",
                time.localtime((s.get("ended_at") or s.get("started_at") or 0) / 1000),
            ) if (s.get("ended_at") or s.get("started_at")) else "?"
            bits = [f"{s['agent']}"]
            if s.get("git_branch"):
                bits.append(s["git_branch"])
            if s.get("file_count"):
                bits.append(f"{s['file_count']} files")
            head = f"- [{when}] {' · '.join(bits)}: {s.get('title') or '(untitled)'}"
            lines.append(head)
            summary = (s.get("summary") or "").strip()
            if summary:
                first = summary.splitlines()
                for extra in first[:3]:
                    lines.append(f"    {extra}")
            for f in s.get("top_files", []):
                files[f] = files.get(f, 0) + 1
        if files:
            top = sorted(files.items(), key=lambda kv: -kv[1])[:8]
            lines.append("Recently touched files: " + ", ".join(f for f, _ in top))
        return "\n".join(lines)

    def stats(self) -> dict[str, Any]:
        total = self.conn.execute("SELECT count(*) c FROM sessions").fetchone()["c"]
        events = self.conn.execute("SELECT count(*) c FROM events").fetchone()["c"]
        by_agent = {
            r["agent"]: r["c"]
            for r in self.conn.execute(
                "SELECT agent, count(*) c FROM sessions GROUP BY agent"
            ).fetchall()
        }
        last = self.conn.execute(
            "SELECT max(COALESCE(ended_at, started_at)) m FROM sessions"
        ).fetchone()["m"]
        return {
            "db": str(self.conn.execute("PRAGMA database_list").fetchone()["file"]),
            "sessions": total,
            "events": events,
            "by_agent": by_agent,
            "last_activity": last,
            "embeddings": self.conn.execute(
                "SELECT count(*) c FROM embeddings"
            ).fetchone()["c"],
        }

    def top_files(self, session_id: str, limit: int = 30) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT target, count(*) c FROM events
            WHERE session_id=? AND kind IN ('file_edit','file_write','file_delete')
              AND target IS NOT NULL
            GROUP BY target ORDER BY c DESC LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
        return [r["target"] for r in rows]

    def _row_to_dict(self, r: sqlite3.Row, matched: bool = False) -> dict[str, Any]:
        d = {k: r[k] for k in r.keys()}
        d["top_files"] = self.top_files(r["id"]) if r["file_count"] else []
        d["matched"] = matched
        return d
