from reflect.store.instruction_memory import discover_instruction_files, upsert_instruction_memories
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def test_discover_instruction_files_and_store_memories(tmp_path):
    workspace = tmp_path / "repo"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()

    (workspace / "AGENTS.md").write_text("# project\n", encoding="utf-8")
    (workspace / "CLAUDE.md").write_text("# claude project\n", encoding="utf-8")
    (workspace / ".cursorrules").write_text("# legacy cursor\n", encoding="utf-8")
    (workspace / ".github" / "instructions").mkdir(parents=True)
    (workspace / ".github" / "instructions" / "api.instructions.md").write_text("# api\n", encoding="utf-8")
    (workspace / ".github" / "copilot-instructions.md").write_text("# copilot\n", encoding="utf-8")
    (workspace / ".cursor" / "rules").mkdir(parents=True)
    (workspace / ".cursor" / "rules" / "frontend.md").write_text("# cursor\n", encoding="utf-8")
    (workspace / ".cursor" / "rules" / "backend.mdc").write_text("# cursor mdc\n", encoding="utf-8")
    (workspace / ".cursor" / "agents").mkdir(parents=True)
    (workspace / ".cursor" / "agents" / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")
    (home / ".cursor" / "plans").mkdir(parents=True)
    (home / ".cursor" / "plans" / "workflow.plan.md").write_text("# plan\n", encoding="utf-8")
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "CLAUDE.md").write_text("# user claude\n", encoding="utf-8")
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "GEMINI.md").write_text("# gemini\n", encoding="utf-8")

    discovered = discover_instruction_files(workspace, home_root=home)
    discovered_names = {path.name for path in discovered}

    assert {
        "AGENTS.md",
        "CLAUDE.md",
        ".cursorrules",
        "copilot-instructions.md",
        "frontend.md",
        "backend.mdc",
        "reviewer.md",
        "workflow.plan.md",
        "api.instructions.md",
        "GEMINI.md",
    } <= discovered_names

    db = tmp_path / "reflect.db"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        result = upsert_instruction_memories(conn, workspace_root=workspace, home_root=home)
        assert result["discovered"] >= 7
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == result["discovered"]

        rows = conn.execute(
            "SELECT scope, type, source, content_hash, content_preview_redacted, raw_attrs_json FROM memories ORDER BY type, scope"
        ).fetchall()
        scopes = {(row[0], row[1]) for row in rows}
        assert ("user", "claude_memory") in scopes
        assert ("user", "gemini_memory") in scopes
        assert ("project", "claude_memory") in scopes
        assert ("project", "copilot_instruction") in scopes
        assert ("project", "agent_instruction") in scopes
        assert ("path", "cursor_rule") in scopes
        assert ("path", "cursor_agent") in scopes
        assert ("user", "cursor_plan") in scopes
        assert any(row[2] == "filesystem_instruction_scan" for row in rows)
        assert all(row[3] for row in rows)
        assert all(row[4] for row in rows)
        assert all('"path"' in row[5] for row in rows)
    finally:
        conn.close()
