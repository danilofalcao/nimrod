#!/bin/sh
# Bootstrap Nimrod after a fresh machine / fresh clone.
#
# After formatting the computer, reinstall the agents (Claude Code, Codex,
# OpenCode, Pi) and then run this once:
#
#     git clone <remote> ~/src/nimrod
#     cd ~/src/nimrod
#     ./bootstrap.sh
#
# It creates the virtualenv, installs the package, wires the hooks + MCP into
# the three agents, and backfills history. Idempotent: re-run it after a `git
# pull`, after the repo moves, or when an agent was reinstalled.
set -eu

repo="$(cd "$(dirname "$0")" && pwd)"
cd "$repo"

python=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    python="$candidate"
    break
  fi
done
if [ -z "$python" ]; then
  echo "python3 not found; install Python >= 3.11 first" >&2
  exit 1
fi

if [ ! -x "$repo/.venv/bin/python" ]; then
  echo "creating virtualenv at $repo/.venv"
  "$python" -m venv "$repo/.venv"
fi

"$repo/.venv/bin/python" -m pip install --quiet --upgrade pip
echo "installing nimrod (this pulls mcp + fastembed the first time)"
"$repo/.venv/bin/python" -m pip install --quiet -e "$repo"

export NIMROD_BIN="$repo/.venv/bin/nimrod"
"$NIMROD_BIN" install

echo
echo "backfilling existing agent history"
"$NIMROD_BIN" ingest --force --quiet || true
"$NIMROD_BIN" embed || true

echo
echo "Nimrod ready. Restart Claude Code / Codex / OpenCode / Pi so hooks and MCP reload."
