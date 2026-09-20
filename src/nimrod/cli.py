"""Command-line interface for the Nimrod worklog."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from . import __version__, db
from .config import Config
from .embeddings import Embedder
from .engine import ingest as run_ingest
from .store import Store


def _config(args) -> Config:
    cfg = Config()
    if getattr(args, "home", None):
        cfg = Config(home=Path(args.home).expanduser())
    return cfg


def _open(cfg: Config, semantic: bool | None = None) -> tuple:
    cfg.ensure()
    conn = db.connect(cfg.db_path)
    embedder = Embedder(
        model_name=cfg.embedding_model,
        enabled=cfg.semantic_enabled if semantic is None else semantic,
        cache_dir=str(cfg.cache_dir),
    )
    return conn, Store(conn, embedder)


def _project(args) -> str | None:
    return getattr(args, "project", None) or os.getcwd()


def _print_sessions(sessions: list[dict], as_json: bool) -> None:
    if as_json:
        print(json.dumps(sessions, ensure_ascii=False, indent=2))
        return
    if not sessions:
        print("(nothing found)")
        return
    for s in sessions:
        when = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime((s.get("ended_at") or s.get("started_at") or 0) / 1000),
        ) if (s.get("ended_at") or s.get("started_at")) else "?"
        print(f"[{when}] {s['agent']:<8} {s.get('project_name') or '?':<20} "
              f"{(s.get('title') or '')[:80]}")
        if s.get("summary"):
            for line in s["summary"].splitlines()[:2]:
                print(f"    {line[:110]}")


# ---------------------------------------------------------------- commands


def cmd_ingest(args) -> int:
    cfg = _config(args)
    report = run_ingest(
        cfg,
        force=args.force,
        agents=args.agent or None,
        project=args.project or None,
        semantic=False if args.no_semantic else None,
        progress=(lambda m: print(m, file=sys.stderr)) if not args.quiet else None,
    )
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"scanned={report.scanned} changed={report.changed} "
              f"embedded={report.embedded} by_agent={report.per_agent}")
    return 0


def cmd_search(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False if args.no_semantic else None)
    try:
        since = int((time.time() - args.since_days * 86400) * 1000) if args.since_days else None
        results = store.search(
            " ".join(args.query),
            project=args.project or None,
            agent=args.agent,
            since=since,
            limit=args.limit,
        )
        _print_sessions(results, args.json)
    finally:
        conn.close()
    return 0


def cmd_recent(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False)
    try:
        _print_sessions(store.recent(_project(args), agent=args.agent, limit=args.limit),
                        args.json)
    finally:
        conn.close()
    return 0


def cmd_timeline(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False)
    try:
        _print_sessions(store.timeline(_project(args), limit=args.limit), args.json)
    finally:
        conn.close()
    return 0


def cmd_context(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False)
    try:
        text = store.context(_project(args), limit=args.limit)
        print(text or "(no recorded work for this project)")
    finally:
        conn.close()
    return 0


def cmd_session(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False)
    try:
        data = store.get_session(args.session_id)
        if data is None:
            print("not found", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print(f"{data['agent']} · {data.get('project_name')} · {data.get('title')}")
            print(f"started={data.get('started_at')} ended={data.get('ended_at')} "
                  f"files={data.get('file_count')} tools={data.get('tool_count')}")
            print()
            print(data.get("summary") or "")
            print()
            for e in data.get("events", []):
                target = f" -> {e['target']}" if e.get("target") else ""
                print(f"  [{e['kind']}]{target} {(e.get('text') or '')[:100]}")
    finally:
        conn.close()
    return 0


def cmd_stats(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=False)
    try:
        print(json.dumps(store.stats(), ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


def cmd_embed(args) -> int:
    cfg = _config(args)
    conn, store = _open(cfg, semantic=True)
    try:
        if not store.embedder.enabled:
            print("embeddings disabled (fastembed unavailable or NIMROD_NO_EMBEDDINGS=1)",
                  file=sys.stderr)
            return 1
        pruned = store.prune_embeddings()
        if pruned:
            print(f"pruned {pruned} embeddings from an older model", file=sys.stderr)
        total = 0
        while True:
            ids = store.pending_embedding_ids(limit=args.batch)
            if not ids:
                break
            total += store.embed_sessions(ids)
            if total >= args.limit:
                break
            if len(ids) < args.batch:
                break
            print(f"embedded {total}…", file=sys.stderr)
        print(f"embedded {total} sessions")
    finally:
        conn.close()
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import create_server

    create_server().run()
    return 0


def cmd_install(args) -> int:
    from .install import install_all

    install_all(Config(), dry=args.dry_run)
    return 0

TOOLS_HINT = (
    "Nimrod work memory is available in this session, shared across Claude, "
    "Codex, OpenCode and Pi: work_search(query) finds how something was done "
    "before, work_context(project) returns a project brief, work_recent and "
    "work_timeline list history, work_session(id) shows one session. Project "
    "arguments accept a path, a folder name, or '*' for all projects. Prefer "
    "these over querying the database by hand."
)


def _tokens(text: str, limit: int = 12) -> list[str]:
    """Language-agnostic token extraction: words of length >= 3, de-duplicated."""
    out: list[str] = []
    for token in re.findall(r"\w+", text.lower()):
        if len(token) < 3 or token.isdigit():
            continue
        if token not in out:
            out.append(token)
        if len(out) >= limit:
            break
    return out


def _prompt_submit(args, cfg: Config) -> int:
    """Economical, language-agnostic auto-recall.

    No keyword or stopword lists anywhere. The only signal used is whether the
    prompt names a project that the worklog already knows about and that is not
    the one the agent is currently in -- e.g. asking about ``wfkit-archinstall``
    from the ``nimrod`` repo. Project names come from the database, so this
    works in any language. Everything else stays on demand via ``work_search``.
    """
    if os.environ.get("NIMROD_AUTO_RECALL") == "0":
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    prompt = (payload.get("prompt") or payload.get("message") or "").strip()
    if len(prompt) < 12:
        return 0
    tokens = set(_tokens(prompt, limit=24))
    if not tokens:
        return 0
    cwd = payload.get("cwd") or payload.get("project_dir") or args.project
    cwd = (cwd or "").rstrip("/")

    conn, store = _open(cfg, semantic=False)
    try:
        matches: list[tuple[int, dict]] = []
        for project in store.project_index():
            name = (project["name"] or "").strip()
            if len(name) < 3:
                continue
            path = (project["path"] or "").rstrip("/")
            if cwd and path == cwd:
                continue
            name_tokens = _tokens(name.replace("-", " ").replace("_", " "), limit=6)
            hit = len(tokens & set(name_tokens))
            if hit:
                matches.append((hit, project))
        if not matches:
            return 0
        matches.sort(key=lambda kv: (-kv[0], -(kv[1]["last_seen"] or 0)))
        best = matches[0][1]
        hits = store.recent(best["path"] or best["name"], limit=3)
    finally:
        conn.close()
    if not hits:
        return 0

    session_id = str(payload.get("session_id") or "")
    marker = cfg.cache_dir / f"recall-{session_id}.txt" if session_id else None
    if marker is not None and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == best["name"]:
                return 0
        except OSError:
            pass

    lines = [f"Nimrod has past work for project '{best['name']}' "
             "(share only if relevant; use work_session(id) for details):"]
    for s in hits:
        when = time.strftime(
            "%Y-%m-%d", time.localtime((s.get("ended_at") or s.get("started_at") or 0) / 1000)
        ) if (s.get("ended_at") or s.get("started_at")) else "?"
        lines.append(f"- [{when} {s['agent']}] {(s.get('title') or '')[:90]} "
                     f"(id: {s['id']})")
    if marker is not None:
        try:
            marker.write_text(best["name"], encoding="utf-8")
        except OSError:
            pass
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }, ensure_ascii=False))
    return 0


def cmd_hook(args) -> int:
    """Entry point used by the Claude/Codex/OpenCode/Pi integrations.

    ``session-start`` refreshes the log then emits context in the shape the
    calling CLI expects; ``session-end`` only refreshes.
    """
    cfg = _config(args)
    if args.event == "prompt-submit":
        return _prompt_submit(args, cfg)
    quiet = args.event != "session-start"
    try:
        run_ingest(
            cfg,
            project=args.project or None,
            semantic=bool(args.semantic),
            progress=None,
        )
    except Exception:
        pass  # never block an agent session because ingestion failed

    if args.event == "session-end":
        return 0

    conn, store = _open(cfg, semantic=False)
    try:
        brief = store.context(args.project or os.getcwd(), limit=args.limit)
    finally:
        conn.close()
    text = TOOLS_HINT if not brief else TOOLS_HINT + "\n\n" + brief

    if args.agent == "claude":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }
        print(json.dumps(payload, ensure_ascii=False))
    elif args.agent == "codex":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": text,
            }
        }, ensure_ascii=False))
    else:
        if not quiet:
            print(text)
    return 0


def _add_common(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("--home", help="override NIMROD_HOME")
    sp.add_argument("--json", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nimrod", description="Cross-session work memory")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="scan agent stores and update the worklog")
    _add_common(p)
    p.add_argument("--force", action="store_true", help="reparse everything")
    p.add_argument("--agent", action="append", choices=["claude", "codex", "opencode", "pi"])
    p.add_argument("--project", help="only sessions under this path")
    p.add_argument("--no-semantic", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("search", help="hybrid search over past work")
    _add_common(p)
    p.add_argument("query", nargs="+")
    p.add_argument("--project")
    p.add_argument("--agent", choices=["claude", "codex", "opencode", "pi"])
    p.add_argument("--since-days", type=float)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--no-semantic", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("recent", help="most recent sessions")
    _add_common(p)
    p.add_argument("--project")
    p.add_argument("--agent", choices=["claude", "codex", "opencode", "pi"])
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_recent)

    p = sub.add_parser("timeline", help="chronological sessions for a project")
    _add_common(p)
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=25)
    p.set_defaults(func=cmd_timeline)

    p = sub.add_parser("context", help="print the handoff brief for a project")
    _add_common(p)
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=6)
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("session", help="show one session in detail")
    _add_common(p)
    p.add_argument("session_id")
    p.set_defaults(func=cmd_session)

    p = sub.add_parser("stats", help="database statistics")
    _add_common(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("embed", help="compute embeddings for sessions missing them")
    _add_common(p)
    p.add_argument("--limit", type=int, default=100000)
    p.add_argument("--batch", type=int, default=64)
    p.set_defaults(func=cmd_embed)

    p = sub.add_parser("serve", help="run the MCP server over stdio")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("install", help="wire hooks and MCP into the supported agents")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("hook", help="internal: invoked by agent integrations")
    p.add_argument("event", choices=["session-start", "session-end", "prompt-submit"])
    p.add_argument("--agent", default="claude", choices=["claude", "codex", "opencode", "pi"])
    p.add_argument("--project")
    p.add_argument("--limit", type=int, default=6)
    p.add_argument("--semantic", action="store_true",
                   help="also compute embeddings during ingest (slower)")
    p.add_argument("--home")
    p.set_defaults(func=cmd_hook)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
