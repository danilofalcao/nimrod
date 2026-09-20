#!/bin/sh
# Nimrod agent hook (installed by `nimrod install`).
# Usage: nimrod-hook.sh <session-start|session-end> <claude|codex|opencode>
#
# Reads the hook payload from stdin, finds the working directory, refreshes the
# worklog and (on session start) prints context in the shape the calling agent
# expects. It never fails the agent: all errors are swallowed.

event="${1:-session-start}"
agent="${2:-claude}"
NIMROD_BIN="${NIMROD_BIN:-__NIMROD_BIN__}"

input=""
if [ ! -t 0 ]; then
  input="$(cat 2>/dev/null || true)"
fi

project=""
if [ -n "$input" ] && command -v python3 >/dev/null 2>&1; then
  project="$(printf '%s' "$input" | python3 -c 'import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    data = {}
for key in ("cwd", "project_dir", "working_directory", "directory", "project_path"):
    value = data.get(key)
    if isinstance(value, str) and value:
        print(value)
        break
' 2>/dev/null || true)"
fi
[ -n "$project" ] || project="${PWD:-$HOME}"

if [ "$event" = "session-end" ]; then
  # Refresh (with embeddings) in the background so the agent is never blocked.
  ( "$NIMROD_BIN" hook session-end --agent "$agent" --project "$project" --semantic \
      >/dev/null 2>&1 & ) || true
  exit 0
fi

if [ "$event" = "prompt-submit" ]; then
  # Cheap, gated auto-recall: the CLI decides whether this prompt is worth
  # searching and only then prints context. No ingest here.
  printf '%s' "$input" | "$NIMROD_BIN" hook prompt-submit --agent "$agent" \
      --project "$project" 2>/dev/null || true
  exit 0
fi

"$NIMROD_BIN" hook "$event" --agent "$agent" --project "$project" 2>/dev/null || true
exit 0
