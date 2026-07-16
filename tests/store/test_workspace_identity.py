import json
from pathlib import Path

from reflect.store.graph_normalize import rebuild_graph
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.store.workspaces import WorkspaceResolver, backfill_session_context
from reflect.views.report_tabs import _semantic_graph


def _insert_session(conn, session_id, now, *, source_ref="", agent_id=None):
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, ended_at, status, source_ref, created_at, updated_at
        ) VALUES (?, ?, ?, ?, 'ok', ?, ?, ?)
        """,
        (session_id, agent_id, now, now, source_ref, now, now),
    )


def _insert_step(conn, session_id, step_id, seq, now, attrs):
    conn.execute(
        """
        INSERT INTO steps(
          id, session_id, seq, type, started_at, ended_at, status,
          raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'tool_call', ?, ?, 'ok', ?, ?, ?)
        """,
        (step_id, session_id, seq, now, now, json.dumps(attrs), now, now),
    )


def _insert_tool(conn, session_id, step_id, tool_id, now, path):
    conn.execute(
        """
        INSERT INTO tool_calls(
          id, step_id, session_id, tool_name, tool_type, status,
          input_preview_redacted, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, 'Read', 'file', 'ok', ?, ?, ?, ?)
        """,
        (
            tool_id,
            step_id,
            session_id,
            json.dumps({"file_path": path}),
            json.dumps({"gen_ai.client.file_path": path}),
            now,
            now,
        ),
    )


def test_workspace_resolver_prefers_specific_git_root(tmp_path):
    repo = tmp_path / "group" / "reflect"
    (repo / ".git").mkdir(parents=True)

    identity = WorkspaceResolver().resolve({
        "code.workspace.root": str(tmp_path / "group"),
        "gen_ai.client.workspace": str(repo),
    })

    assert identity is not None
    assert identity.root_path == str(repo)
    assert identity.repo_root == str(repo)
    assert identity.confidence == 0.98


def test_workspace_resolver_rejects_root_and_home_context():
    resolver = WorkspaceResolver()

    assert resolver.resolve({"code.workspace.root": "/"}) is None
    assert resolver.resolve({"code.workspace.root": str(Path.home())}) is None


def test_backfill_and_graph_share_workspace_paths_and_parent_lineage(tmp_path):
    db = tmp_path / "reflect.db"
    repo = tmp_path / "reflect"
    (repo / ".git").mkdir(parents=True)
    now = "2026-07-15T10:00:00+00:00"
    conn = connect_sqlite(db)
    try:
        migrate(conn)
        conn.executemany(
            """
            INSERT INTO agents(id, name, kind, raw_json, created_at, updated_at)
            VALUES (?, ?, 'coding_agent', '{}', ?, ?)
            """,
            [("codex", "codex", now, now), ("claude", "claude", now, now)],
        )
        _insert_session(conn, "parent", now, agent_id="codex")
        _insert_session(
            conn,
            "child",
            now,
            source_ref=f"native_session:cursor:{repo}/agent-transcripts/parent/subagents/child.jsonl",
            agent_id="codex",
        )
        _insert_session(conn, "claude-peer", now, agent_id="claude")
        for session_id in ("parent", "child", "claude-peer"):
            step_id = f"step-{session_id}"
            attrs = {"code.workspace.root": str(repo)}
            _insert_step(conn, session_id, step_id, 1, now, attrs)
            _insert_tool(conn, session_id, step_id, f"tool-{session_id}-1", now, str(repo / "src/app.py"))
        _insert_tool(conn, "parent", "step-parent", "tool-parent-2", now, str(repo / "src/app.py"))
        conn.commit()

        changed: set[str] = set()
        result = backfill_session_context(
            conn,
            timestamp=now,
            changed_session_ids=changed,
        )
        rebuild_graph(conn)

        assert result["sessions_updated"] == 3
        assert result["workspaces"] == 1
        assert result["parents"] == 1
        assert changed == {"parent", "child", "claude-peer"}
        session_context = conn.execute(
            "SELECT id, workspace_id, repo_id, parent_session_id FROM sessions ORDER BY id"
        ).fetchall()
        assert len({row[1] for row in session_context}) == 1
        assert len({row[2] for row in session_context}) == 1
        child = next(row for row in session_context if row[0] == "child")
        assert child[3] == "parent"

        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Workspace'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Path' AND label = 'src/app.py' AND session_id IS NULL"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Folder' AND label = 'src' AND session_id IS NULL"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE kind = 'ran_in_workspace'"
        ).fetchone()[0] == 3
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE kind = 'spawned_session'"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT MAX(weight) FROM graph_edges WHERE kind = 'touched_folder' AND session_id = 'parent'"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE kind = 'Folder' AND label IN ('**', '/Users')"
        ).fetchone()[0] == 0

        graph = _semantic_graph(conn, None)
        assert any(node["kind"] == "Workspace" for node in graph["nodes"])
        assert any(edge["kind"] == "ran_in_workspace" for edge in graph["edges"])
        scoped = _semantic_graph(conn, ["child"])
        assert {node["session_id"] for node in scoped["nodes"] if node["kind"] == "Session"} == {
            "parent",
            "child",
            "claude-peer",
        }
        assert {
            node["label"] for node in scoped["nodes"] if node["kind"] == "Agent"
        } == {"codex", "claude"}
        assert any(edge["kind"] == "spawned_session" for edge in scoped["edges"])
    finally:
        conn.close()


def test_preview_path_extraction_does_not_turn_shell_globs_into_folders(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    now = "2026-07-15T10:00:00+00:00"
    try:
        migrate(conn)
        _insert_session(conn, "session", now)
        _insert_step(conn, "session", "step", 1, now, {})
        conn.execute(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, tool_type, status,
              input_preview_redacted, raw_attrs_json, created_at, updated_at
            ) VALUES ('tool', 'step', 'session', 'exec_command', 'shell', 'ok', ?, '{}', ?, ?)
            """,
            ('{"cmd":"rg **/SKILL.md and report CI/UI status"}', now, now),
        )
        conn.commit()

        rebuild_graph(conn)

        labels = {row[0] for row in conn.execute("SELECT label FROM graph_nodes WHERE kind = 'Folder'")}
        assert "**" not in labels
        assert "CI" not in labels
        assert "UI" not in labels
    finally:
        conn.close()
