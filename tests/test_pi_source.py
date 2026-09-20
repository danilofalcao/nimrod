import json

from nimrod.sources.pi import parse_file


def _write(path, entries):
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_pi_parses_prompts_tools_and_metadata(tmp_path):
    session = tmp_path / "2026-09-20T00-00-00-000Z_abc123.jsonl"
    _write(session, [
        {"type": "session", "version": 3, "id": "abc123",
         "timestamp": "2026-09-20T00:00:00.000Z", "cwd": "/home/me/proj"},
        {"type": "model_change", "id": "m1", "parentId": None,
         "timestamp": "2026-09-20T00:00:00.100Z",
         "provider": "deepseek", "modelId": "deepseek-flash"},
        {"type": "session_info", "id": "s1", "parentId": "m1",
         "timestamp": "2026-09-20T00:00:00.200Z", "name": "Fix the widget"},
        {"type": "message", "id": "u1", "parentId": "s1",
         "timestamp": "2026-09-20T00:00:01.000Z",
         "message": {"role": "user", "content": "please fix the widget",
                     "timestamp": 1758326401000}},
        {"type": "message", "id": "a1", "parentId": "u1",
         "timestamp": "2026-09-20T00:00:02.000Z",
         "message": {
             "role": "assistant", "model": "deepseek-flash",
             "content": [
                 {"type": "thinking", "thinking": "hmm"},
                 {"type": "text", "text": "On it."},
                 {"type": "toolCall", "id": "t1", "name": "read",
                  "arguments": {"path": "/home/me/proj/widget.py"}},
                 {"type": "toolCall", "id": "t2", "name": "edit",
                  "arguments": {"path": "/home/me/proj/widget.py"}},
                 {"type": "toolCall", "id": "t3", "name": "bash",
                  "arguments": {"command": "pytest -q"}},
             ],
             "usage": {"input": 10, "output": 5, "cacheRead": 2,
                       "cacheWrite": 1, "totalTokens": 18,
                       "cost": {"total": 0.01}},
             "stopReason": "toolUse", "timestamp": 1758326402000,
         }},
        {"type": "message", "id": "r1", "parentId": "a1",
         "timestamp": "2026-09-20T00:00:03.000Z",
         "message": {"role": "toolResult", "toolCallId": "t3", "toolName": "bash",
                     "content": [{"type": "text", "text": "1 failed"}],
                     "isError": True, "timestamp": 1758326403000}},
        {"type": "message", "id": "b1", "parentId": "r1",
         "timestamp": "2026-09-20T00:00:04.000Z",
         "message": {"role": "bashExecution", "command": "git status",
                     "output": "", "exitCode": 0, "cancelled": False,
                     "truncated": False, "timestamp": 1758326404000}},
        {"type": "compaction", "id": "c1", "parentId": "b1",
         "timestamp": "2026-09-20T00:00:05.000Z", "summary": "so far",
         "firstKeptEntryId": "u1", "tokensBefore": 100},
    ])

    parsed = parse_file(session)
    assert parsed is not None
    assert parsed.session_id == "abc123"
    assert parsed.project_path == "/home/me/proj"
    assert parsed.model == "deepseek-flash"
    assert parsed.title == "Fix the widget"
    assert parsed.prompt_count == 1
    assert parsed.started_at is not None and parsed.ended_at is not None
    assert parsed.tokens_input == 13
    assert parsed.tokens_output == 5
    assert abs(parsed.cost - 0.01) < 1e-9

    kinds = [e.kind for e in parsed.events]
    assert "prompt" in kinds
    assert "assistant" in kinds
    assert "file_read" in kinds
    assert "file_edit" in kinds
    assert "command" in kinds
    assert "error" in kinds
    assert "note" in kinds

    targets = {e.target for e in parsed.events}
    assert "/home/me/proj/widget.py" in targets
    assert "pytest -q" in targets
    assert "git status" in targets


def test_pi_falls_back_to_filename_when_header_missing(tmp_path):
    session = tmp_path / "2026-09-20T00-00-00-000Z_deadbeef.jsonl"
    _write(session, [
        {"type": "message", "id": "u1", "parentId": None,
         "timestamp": "2026-09-20T00:00:01.000Z",
         "message": {"role": "user", "content": "hello there",
                     "timestamp": 1758326401000}},
    ])
    parsed = parse_file(session)
    assert parsed is not None
    assert parsed.session_id == "deadbeef"
    assert parsed.prompt_count == 1
    assert parsed.events[0].kind == "prompt"
