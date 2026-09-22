# Nimrod — Cross-session work memory for coding agents

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)

Cross-session work memory shared by Claude Code, Codex, OpenCode and Pi. It reads each agent's local session store (read-only), normalizes it into a single worklog, advertises itself at session start (without injecting the worklog), and exposes hybrid (lexical + semantic) recall through MCP.

When you work across agents — Claude Code today, Codex tomorrow, OpenCode or Pi in between — each agent currently starts cold. Nimrod fixes that.

---

## What Nimrod does

1. **Automatic capture.** Hooks (and, for Pi, a TypeScript extension) invoke Nimrod at the start and end of sessions. Ingest reads each agent's local session store in read-only mode — no model is involved in capture; extraction is fully deterministic.
   - Claude Code: `~/.claude/projects/<slug>/*.jsonl`
   - Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` (+ `~/.codex/state_5.sqlite`)
   - OpenCode: `~/.local/share/opencode/opencode.db` (SQLite)
   - Pi: `~/.pi/agent/sessions/.../*.jsonl`
2. **A single worklog.** Every session becomes one normalized record (agent, project, title, summary, events: prompts, file edits, commands, outcomes) in `~/.nimrod/work.db` (SQLite), plus local embeddings for semantic search.
3. **Awareness on first call.** When you open a session, Nimrod tells the model that the worklog exists and when to consult it — but injects no worklog content. The model reaches for `work_context` or `work_search` when it actually lacks context, so a greeting never triggers a recall.
4. **On-demand recall (MCP).** Any MCP-capable agent can call `work_search` (hybrid: lexical + semantic), `work_context`, `work_recent`, `work_timeline` and `work_session` at any time.
5. **Gated auto-recall.** When a prompt mentions a project the worklog already knows about (and it is not the project you are in), Nimrod appends a short list of that other project's most recent sessions — once per prompt, with no keyword lists involved.

Everything is **local**: no cloud, no external services, and the embedding model runs on your machine via FastEmbed.

---

## How it works

```
 Claude Code / Codex / OpenCode / Pi
        │  hooks (sh/JS/TS) call  nimrod hook
        ▼
  ingest ─────────────────────► ingest reads local agent stores (read-only)
        │                      ▼
        │               ~/.nimrod/work.db (SQLite)
        │                      + ~/.nimrod/cache (embeddings)
        ▼
 awareness notice on the first model call  ◄────────  MCP: work_search /
        │                                          work_context / ...
        ▼
 agent consults Nimrod when it needs context
```

The `SessionEnd` hook (and Pi's `session_shutdown`) also re-runs ingest *with embeddings*, in the background, so the next session has everything available.

---

## Supported agents

| Agent      | Session start                                  | Capture                                              | MCP registration                     |
|------------|------------------------------------------------|------------------------------------------------------|--------------------------------------|
| Claude Code| Hooks in `~/.claude/settings.json`             | `session-start` + `session-end` + `prompt-submit`    | `claude mcp add` (user scope)        |
| Codex      | Hooks in `~/.codex/hooks.json`                 | same                                                 | `codex mcp add`                      |
| OpenCode   | Plugin `~/.config/opencode/plugins/nimrod-context.js` | on the next session open (the plugin triggers ingest) | `opencode.jsonc` (an `mcp` block) |
| Pi         | TS extension `extensions/nimrod.ts`            | `session_start` + `session_shutdown` (background)    | `~/.pi/agent/mcp.json` + `pi-mcp-adapter` package |

---

## Installation

### Requirements

- Python **>= 3.11** (tested on 3.11 – 3.14)
- The agents installed: Claude Code (`claude`), Codex (`codex`), OpenCode, and Pi
- Node, for Pi only (the extension is TypeScript); the `pi-mcp-adapter` package is included automatically

### Recommended: `./bootstrap.sh`

Ideal for a fresh machine, a fresh clone, or after a `git pull` / agent reinstall. **It is idempotent.**

```sh
git clone https://github.com/danilofalcao/nimrod
cd nimrod
./bootstrap.sh
```

The script:

1. Creates a virtualenv at `.venv/` (if missing)
2. Installs the package in editable mode (fetches `mcp` and `fastembed` the first time)
3. Runs `nimrod install` — wires hooks/extension and MCP into every agent
4. Backfills history: `nimrod ingest --force` + `nimrod embed`
5. Tells you to restart the agents

### Manual

```sh
git clone https://github.com/danilofalcao/nimrod
cd nimrod
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/nimrod install
```

`nimrod install` (idempotent, writes a `.nimrod.bak` backup before touching any file) does the following:

| Agent       | Files touched                                                            |
|-------------|--------------------------------------------------------------------------|
| Shared      | `~/.hooks/nimrod-hook.sh` (shared hook script; uses `NIMROD_BIN`)        |
| Claude Code | `~/.claude/settings.json` — `SessionStart` (25 s), `SessionEnd` (3 s, background), `UserPromptSubmit` (10 s) + user-scoped MCP |
| Codex       | `~/.codex/hooks.json` — the three events + MCP                           |
| OpenCode    | `~/.config/opencode/plugins/nimrod-context.js` + an `"mcp"` block in `~/.config/opencode/opencode.jsonc` |
| Pi          | `$PI_CODING_AGENT_DIR/extensions/nimrod.ts` (default `~/.pi/agent/extensions/nimrod.ts`) + `~/.pi/agent/mcp.json` + ensures `pi-mcp-adapter` is in `settings.json` |

Preview what will be written without touching anything:

```sh
nimrod install --dry-run
```

### After installation

1. **Restart** Claude Code, Codex, OpenCode, and/or Pi (hooks, the extension and MCP only reload then).
2. **In Codex:** run `/hooks` — Codex marks new/changed hooks as untrusted; trust `nimrod-hook.sh`.
3. (Already done by `bootstrap.sh` — if you installed manually:) backfill history.

---

## Try it

```sh
nimrod context                     # the handoff brief for the current project
nimrod context --project other-repo  # ... for another project, by folder name
nimrod search "race condition in the parser"
nimrod recent --agent codex
nimrod stats
```

---

## MCP tools

Exposed by `nimrod serve` (stdio). Registration is done by `nimrod install`.

| Tool | Use |
|------|-----|
| `work_search(query, project?, agent?, since_days?, limit?)` | Hybrid (lexical + semantic) search over prompts, summaries, file paths, commands and outcomes. Returns sessions ranked by relevance, not date. |
| `work_context(project?, limit?)` | A compact project brief. Call it when you need the current project's recent context, or when the user names another project. |
| `work_recent(project?, agent?, limit?, since?, until?, detail?)` | Most recent sessions, newest first. `since`/`until` bound the window (ISO date/datetime on the local clock, or epoch ms); `until` includes the named day. Brief rows by default; `detail=true` adds summaries. |
| `work_timeline(project?, limit?, since?, until?, detail?)` | Project sessions in chronological order, with the same window and detail arguments. |
| `work_session(id)` | Full detail of one session, including its normalized events. |
| `work_ingest(force?)` | Re-scans local agent stores and updates the worklog. |
| `work_stats()` | Database statistics and coverage. |
| `work_record(note, project?, title?)` | Record a manual note (optional — capture is automatic). |

The `project` argument accepts a path, a project folder name, or `*` for every project. Filters compose: `work_recent(project='*', since='2026-09-21', until='2026-09-21')` returns everything from that day across every project, so an enumeration never needs the database.

---

## CLI

Available commands (`nimrod --help` for full flags):

| Command | Description |
|---------|-------------|
| `nimrod ingest [--force] [--agent X]... [--project P] [--no-semantic] [--quiet] [--json]` | Reads agent stores and updates the worklog. |
| `nimrod search <query>... [--project] [--agent] [--since-days] [--limit] [--no-semantic] [--json]` | Hybrid search over the worklog. |
| `nimrod recent [--project] [--agent] [--limit] [--json]` | Most recent sessions. |
| `nimrod timeline [--project] [--limit]` | Project chronological list. |
| `nimrod context [--project] [--limit]` | The project brief (what the model fetches via `work_context`). |
| `nimrod session <id> [--json]` | Full detail of one session. |
| `nimrod stats [--json]` | Worklog statistics. |
| `nimrod embed [--batch] [--limit]` | Compute embeddings for pending sessions. |
| `nimrod serve` | Run the MCP server on stdio. |
| `nimrod install [--dry-run]` | Wire hooks/extension and MCP into the agents. |
| `nimrod hook <event> --agent A --project P [--semantic]` | Internal entry point used by the integrations. |

---

## Where your data lives (all local)

| Path | Content |
|------|---------|
| `~/.nimrod/work.db` | The worklog (SQLite). |
| `~/.nimrod/cache` | Embedding cache. |
| `~/.hooks/nimrod-hook.sh` | Shared hook script (Claude/Codex/OpenCode). |

---

## Environment variables

| Variable | Effect |
|----------|--------|
| `NIMROD_HOME` | Where the worklog and cache live (default `~/.nimrod`). |
| `NIMROD_NO_EMBEDDINGS=1` | Disables semantic search (keeps lexical). Useful when FastEmbed/model is unavailable. |
| `NIMROD_EMBED_MODEL` | Embedding model name (default `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`). |
| `NIMROD_AUTO_RECALL=0` | Disables gated auto-recall on `UserPromptSubmit`. |
| `NIMROD_BIN` | Path to the `nimrod` binary used by hooks (set at install time). |
| `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `OPENCODE_DATA` / `PI_CODING_AGENT_DIR` | Override the default location of each agent's session store. |

---

## Disabling or removing

- **Disable only:** remove the hooks from `~/.claude/settings.json` / `~/.codex/hooks.json`, delete `~/.config/opencode/plugins/nimrod-context.js` and `~/.pi/agent/extensions/nimrod.ts`, and drop the `"nimrod"` entry from `opencode.jsonc` / Pi's `mcp.json`.
- **Remove MCP registration:** `claude mcp remove -s user nimrod` and `codex mcp remove nimrod`.
- **Delete history:** remove `~/.nimrod/`.
- Install-time backups sit next to the original files as `*.nimrod.bak`.

---

## Development

```sh
pip install -e ".[dev]"
pytest -q
```

Tests cover every agent's parser (Claude, Codex, OpenCode, Pi), the store (write, search, context, stats) and Pi install idempotency. CI runs the suite on Python 3.11 – 3.14.

---

## License

[MIT](LICENSE) © Danilo Falcão.
