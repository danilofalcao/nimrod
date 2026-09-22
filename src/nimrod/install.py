"""Wire Nimrod into Claude Code, Codex, OpenCode and Pi.

Per agent, two things get installed:

* a **hook** (or, for Pi, a TypeScript extension) that refreshes the worklog
  and advertises it at session start -- it tells the model the tools exist but
  injects no content (capture is automatic, the model does nothing), and
* the **MCP server** so any agent can query the shared worklog on demand.

Everything writes with a ``.nimrod.bak`` backup and is idempotent. Use
``nimrod install --dry-run`` to preview.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from .config import Config

REPO_HOOKS = Path(__file__).resolve().parent.parent.parent / "hooks"

PI_MCP_PACKAGE = "npm:pi-mcp-adapter"


def _bin() -> str:
    found = shutil.which("nimrod")
    if found:
        return found
    candidate = Path(sys.executable).parent / "nimrod"
    return str(candidate)


def _render(text: str, binary: str) -> str:
    return text.replace("__NIMROD_BIN__", binary)


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.parent / (path.name + ".nimrod.bak"))


def _hook_command(script: Path, event: str, agent: str) -> str:
    return f"bash '{script}' {event} {agent}"


def install_hook_script(config: Config, binary: str, dry: bool) -> Path:
    target_dir = config.home / "hooks"
    target = target_dir / "nimrod-hook.sh"
    source = REPO_HOOKS / "nimrod-hook.sh"
    content = _render(source.read_text(encoding="utf-8"), binary)
    if dry:
        print(f"[dry-run] write {target}")
        return target
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    target.chmod(0o755)
    print(f"wrote {target}")
    return target


def _merge_hooks_json(path: Path, script: Path, agent: str, dry: bool) -> None:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    hooks = data.setdefault("hooks", {})
    events = (
        ("SessionStart", "session-start", 25),
        ("SessionEnd", "session-end", 3),
        ("UserPromptSubmit", "prompt-submit", 10),
    )
    for event, action, timeout in events:
        command = _hook_command(script, action, agent)
        groups = hooks.setdefault(event, [])
        updated = False
        for group in groups:
            for handler in group.get("hooks", []):
                if "nimrod-hook.sh" in handler.get("command", ""):
                    handler["command"] = command
                    handler["timeout"] = timeout
                    updated = True
        if not updated:
            groups.append({
                "hooks": [{"type": "command", "command": command, "timeout": timeout}]
            })
    if dry:
        print(f"[dry-run] merge hooks into {path}")
        return
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"updated {path}")


def install_opencode_plugin(binary: str, dry: bool) -> Path:
    target = Path.home() / ".config" / "opencode" / "plugins" / "nimrod-context.js"
    content = _render((REPO_HOOKS / "opencode-nimrod.js").read_text(encoding="utf-8"), binary)
    if dry:
        print(f"[dry-run] write {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"wrote {target}")
    return target


def _run(cmd: list[str], dry: bool) -> None:
    if dry:
        print(f"[dry-run] {' '.join(cmd)}")
        return
    try:
        subprocess.run(cmd, check=False, capture_output=True, timeout=60)
        print(f"ran {' '.join(cmd)}")
    except Exception as exc:  # pragma: no cover
        print(f"failed {' '.join(cmd)}: {exc}", file=sys.stderr)


def install_claude_mcp(binary: str, dry: bool) -> None:
    if not shutil.which("claude"):
        print("claude not found; skipping Claude MCP registration", file=sys.stderr)
        return
    _run(["claude", "mcp", "remove", "-s", "user", "nimrod"], dry)
    _run(["claude", "mcp", "add", "-s", "user", "nimrod", "--", binary, "serve"], dry)


def install_codex_mcp(binary: str, dry: bool) -> None:
    if not shutil.which("codex"):
        print("codex not found; skipping Codex MCP registration", file=sys.stderr)
        return
    _run(["codex", "mcp", "remove", "nimrod"], dry)
    _run(["codex", "mcp", "add", "nimrod", "--", binary, "serve"], dry)


def install_opencode_mcp(binary: str, dry: bool) -> None:
    path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    if not path.exists():
        alt = path.with_suffix(".json")
        if alt.exists():
            path = alt
        else:
            print(f"{path} not found; skipping OpenCode MCP registration", file=sys.stderr)
            return
    text = path.read_text(encoding="utf-8")
    if '"nimrod"' in text:
        print(f"nimrod already present in {path}")
        return
    if '"mcp"' not in text:
        print(f"could not find an \"mcp\" block in {path}; add it manually", file=sys.stderr)
        return
    entry = (
        '\n    "nimrod": {\n'
        '      "type": "local",\n'
        f'      "command": {json.dumps([binary, "serve"])},\n'
        '      "enabled": true\n'
        "    },"
    )
    idx = text.index('"mcp"')
    brace = text.index("{", idx)
    new_text = text[: brace + 1] + entry + text[brace + 1 :]
    if dry:
        print(f"[dry-run] add nimrod MCP to {path}")
        return
    _backup(path)
    path.write_text(new_text, encoding="utf-8")
    print(f"updated {path}")


def _load_json(path: Path) -> dict | None:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_json(path: Path, data: dict, dry: bool) -> None:
    if dry:
        print(f"[dry-run] write {path}")
        return
    _backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"updated {path}")


def install_pi_extension(config: Config, binary: str, dry: bool) -> Path:
    target = config.pi_root / "extensions" / "nimrod.ts"
    source = REPO_HOOKS / "pi-nimrod.ts"
    content = _render(source.read_text(encoding="utf-8"), binary)
    if dry:
        print(f"[dry-run] write {target}")
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    print(f"wrote {target}")
    return target


def _ensure_pi_mcp_adapter(config: Config, dry: bool) -> None:
    """Pi reads ``mcp.json`` through the ``pi-mcp-adapter`` package."""
    path = config.pi_root / "settings.json"
    data = _load_json(path)
    if data is None:
        print(f"could not parse {path}; add {PI_MCP_PACKAGE!r} to its "
              "\"packages\" list manually", file=sys.stderr)
        return
    packages = data.setdefault("packages", [])
    if any("pi-mcp-adapter" in str(p) for p in packages):
        return
    packages.append(PI_MCP_PACKAGE)
    _save_json(path, data, dry)


def install_pi_mcp(config: Config, binary: str, dry: bool) -> None:
    path = config.pi_root / "mcp.json"
    data = _load_json(path)
    if data is None:
        print(f"could not parse {path}; add the nimrod MCP server manually",
              file=sys.stderr)
        return
    servers = data.setdefault("mcpServers", {})
    entry = {"command": binary, "args": ["serve"]}
    if servers.get("nimrod") == entry:
        print(f"nimrod already present in {path}")
    else:
        servers["nimrod"] = entry
        _save_json(path, data, dry)
    _ensure_pi_mcp_adapter(config, dry)


def install_all(config: Config, dry: bool = False) -> None:
    binary = _bin()
    print(f"nimrod binary: {binary}")
    script = install_hook_script(config, binary, dry)

    _merge_hooks_json(Path.home() / ".claude" / "settings.json", script, "claude", dry)
    _merge_hooks_json(Path.home() / ".codex" / "hooks.json", script, "codex", dry)
    install_opencode_plugin(binary, dry)

    install_claude_mcp(binary, dry)
    install_codex_mcp(binary, dry)
    install_opencode_mcp(binary, dry)
    install_pi_extension(config, binary, dry)
    install_pi_mcp(config, binary, dry)

    if not dry:
        print()
        print("Done. Notes:")
        print("  - Codex marks new/changed hooks as untrusted: run /hooks in Codex to trust it.")
        print("  - Restart Claude Code / Codex / OpenCode / Pi so hooks, extension and MCP reload.")
        print("  - First run backfills history: `nimrod ingest --force` then `nimrod embed`.")
