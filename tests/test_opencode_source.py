import json
import sqlite3

from nimrod.sources.opencode import parse_session


def test_opencode_parses_session(tmp_path):
    db = tmp_path / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session (
            id text PRIMARY KEY, project_id text, workspace_id text, parent_id text,
            slug text, directory text, path text, title text, version text,
            summary_additions integer, summary_deletions integer, summary_files integer,
            cost real, tokens_input integer, tokens_output integer, agent text,
            model text, time_created integer, time_updated integer
        );
        CREATE TABLE message (
            id text PRIMARY KEY, session_id text, time_created integer,
            time_updated integer, data text
        );
        CREATE TABLE part (
            id text PRIMARY KEY, message_id text, session_id text,
            time_created integer, time_updated integer, data text
        );
        """
    )
    conn.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ses_1", "p", None, None, "slug", "/home/me/proj", "", "Fix widget",
         "1.0", 3, 1, 2, 0.5, 100, 20, "build", "deepseek-flash", 1000, 2000),
    )
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("m1", "ses_1", 1000, 1000, json.dumps({"role": "user"})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                 ("p1", "m1", "ses_1", 1000, 1000,
                  json.dumps({"type": "text", "text": "fix the widget"})))
    conn.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                 ("m2", "ses_1", 1500, 1500, json.dumps({"role": "assistant"})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                 ("p2", "m2", "ses_1", 1500, 1500,
                  json.dumps({"type": "tool", "tool": "edit",
                              "state": {"input": {"filePath": "/home/me/proj/widget.py"}}})))
    conn.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                 ("p3", "m2", "ses_1", 1600, 1600,
                  json.dumps({"type": "text", "text": "done"})))
    conn.commit()

    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM session").fetchone()
    parsed = parse_session(conn, row)
    conn.close()

    assert parsed.session_id == "ses_1"
    assert parsed.project_path == "/home/me/proj"
    assert parsed.title == "Fix widget"
    assert parsed.prompt_count == 1
    kinds = [e.kind for e in parsed.events]
    assert kinds == ["prompt", "file_edit", "assistant"]
    assert parsed.events[1].target == "/home/me/proj/widget.py"
