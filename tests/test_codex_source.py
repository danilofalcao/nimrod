import json

from nimrod.sources.codex import merge_segments, parse_rollout


def _write(path, entries):
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_codex_merges_segments_of_same_session(tmp_path):
    meta = {"session_id": "sid-1", "cwd": "/home/me/proj",
            "git": {"branch": "main", "commit_hash": "deadbeef"}}
    first = tmp_path / "rollout-a.jsonl"
    second = tmp_path / "rollout-b.jsonl"
    _write(first, [
        {"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta", "payload": meta},
        {"timestamp": "2026-09-01T10:00:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "start work"}},
        {"timestamp": "2026-09-01T10:00:02Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch",
                     "input": "*** Begin Patch\\n*** Update File: src/a.py\\n@@\\n-x\\n+y\\n*** End Patch"}},
    ])
    _write(second, [
        {"timestamp": "2026-09-01T10:05:00Z", "type": "session_meta", "payload": meta},
        {"timestamp": "2026-09-01T10:05:01Z", "type": "event_msg",
         "payload": {"type": "user_message", "message": "keep going"}},
        {"timestamp": "2026-09-01T10:05:02Z", "type": "event_msg",
         "payload": {"type": "agent_message", "message": "done"}},
    ])

    segments = [s for s in (parse_rollout(first), parse_rollout(second)) if s]
    merged = merge_segments(segments, None)

    assert merged.session_id == "sid-1"
    assert merged.project_path == "/home/me/proj"
    assert merged.git_branch == "main"
    assert merged.prompt_count == 2
    texts = [e.text for e in merged.events if e.kind == "prompt"]
    assert texts == ["start work", "keep going"]
    patch_targets = [e.target for e in merged.events if e.kind == "file_edit"]
    assert patch_targets == ["src/a.py"]
