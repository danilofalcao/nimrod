import json

from nimrod.sources.claude import parse_file


def _write(path, entries):
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_claude_parses_prompts_tools_and_metadata(tmp_path):
    session = tmp_path / "abc123.jsonl"
    _write(session, [
        {"type": "ai-title", "aiTitle": "Fix the widget", "sessionId": "abc123"},
        {
            "type": "user",
            "message": {"role": "user", "content": "please fix the widget"},
            "origin": {"kind": "human"},
            "timestamp": "2026-09-01T10:00:00.000Z",
            "cwd": "/home/me/proj",
            "gitBranch": "main",
            "sessionId": "abc123",
        },
        {
            "type": "assistant",
            "message": {
                "model": "claude-opus-5",
                "usage": {"input_tokens": 10, "output_tokens": 5},
                "content": [
                    {"type": "text", "text": "On it."},
                    {"type": "tool_use", "name": "Write",
                     "input": {"file_path": "/home/me/proj/widget.py"}},
                    {"type": "tool_use", "name": "Bash",
                     "input": {"command": "pytest -q"}},
                ],
            },
            "timestamp": "2026-09-01T10:00:05.000Z",
            "sessionId": "abc123",
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
            "timestamp": "2026-09-01T10:00:06.000Z",
            "sessionId": "abc123",
        },
    ])

    parsed = parse_file(session)
    assert parsed is not None
    assert parsed.session_id == "abc123"
    assert parsed.project_path == "/home/me/proj"
    assert parsed.git_branch == "main"
    assert parsed.model == "claude-opus-5"
    assert parsed.title == "Fix the widget"
    assert parsed.started_at is not None and parsed.ended_at is not None
    assert parsed.prompt_count == 1
    kinds = [e.kind for e in parsed.events]
    assert "prompt" in kinds
    assert "assistant" in kinds
    assert "file_write" in kinds
    assert "command" in kinds
    targets = {e.target for e in parsed.events}
    assert "/home/me/proj/widget.py" in targets
    assert "pytest -q" in targets
