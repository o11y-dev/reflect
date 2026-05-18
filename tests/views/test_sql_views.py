from __future__ import annotations

from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.views.overview import build_overview
from reflect.views.report_tabs import build_report_tabs
from reflect.views.sessions import list_sessions


def _seed_view_db(conn):
    now = "2026-05-01T09:00:00+00:00"
    agents = [
        ("agent-claude", "claude"),
        ("agent-codex", "codex"),
    ]
    conn.executemany(
        """
        INSERT INTO agents(id, name, kind, version, created_at, updated_at)
        VALUES (?, ?, 'cli', 'test', ?, ?)
        """,
        [(agent_id, name, now, now) for agent_id, name in agents],
    )
    conn.execute(
        """
        INSERT INTO repos(id, full_name, created_at, updated_at)
        VALUES ('repo-1', 'example/telemetry-app', ?, ?)
        """,
        (now, now),
    )
    conn.executemany(
        """
        INSERT INTO sessions(
          id, agent_id, repo_id, started_at, ended_at, status, title,
          failure_count, recovered_failure_count, input_tokens, output_tokens,
          estimated_cost_usd, created_at, updated_at
        )
        VALUES (?, ?, 'repo-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "sess-1",
                "agent-claude",
                "2026-05-01T10:00:00+00:00",
                "2026-05-01T10:05:00+00:00",
                "completed",
                "Claude session",
                1,
                1,
                100,
                40,
                0.50,
                now,
                now,
            ),
            (
                "sess-2",
                "agent-codex",
                "2026-05-02T11:00:00+00:00",
                "2026-05-02T11:10:00+00:00",
                "failed",
                "Codex session",
                2,
                0,
                200,
                50,
                0.75,
                now,
                now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO steps(id, session_id, seq, type, started_at, status, created_at, updated_at)
        VALUES (?, ?, ?, 'llm_call', ?, 'completed', ?, ?)
        """,
        [
            ("step-1", "sess-1", 1, "2026-05-01T10:00:00+00:00", now, now),
            ("step-2", "sess-2", 1, "2026-05-02T11:00:00+00:00", now, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO llm_calls(
          id, step_id, session_id, provider, request_model, response_model,
          input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
        )
        VALUES (?, ?, ?, 'openai', ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("llm-1", "step-1", "sess-1", "claude-4.6-opus", "claude-4.6-opus", 100, 40, 0.50, now, now),
            ("llm-2", "step-2", "sess-2", "gpt-5.4", "gpt-5.4", 200, 50, 0.75, now, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO session_rollups(
          session_id, agent, started_at, ended_at, duration_ms, prompt_count,
          tool_call_count, error_count, input_tokens, output_tokens,
          cache_read_tokens, cache_write_tokens, total_cost, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
        """,
        [
            (
                "sess-1",
                "claude",
                "2026-05-01T10:00:00+00:00",
                "2026-05-01T10:05:00+00:00",
                300_000,
                1,
                1,
                1,
                100,
                40,
                0.50,
                now,
            ),
            (
                "sess-2",
                "codex",
                "2026-05-02T11:00:00+00:00",
                "2026-05-02T11:10:00+00:00",
                600_000,
                1,
                2,
                2,
                200,
                50,
                0.75,
                now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO tool_rollups(
          tool_name, agent, call_count, success_count, error_count, total_duration_ms, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("Edit", "codex", 2, 1, 1, 250, now),
            ("Read", "claude", 1, 1, 0, 100, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO tool_calls(
          id, step_id, session_id, tool_name, tool_type, input_preview_redacted,
          status, duration_ms, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'tool', ?, ?, ?, ?, ?)
        """,
        [
            ("tool-1", "step-1", "sess-1", "Read", "{}", "ok", 100, now, now),
            ("tool-2", "step-2", "sess-2", "Edit", '{"cmd":"poetry run pytest"}', "error", 250, now, now),
        ],
    )
    conn.executemany(
        """
        INSERT INTO mcp_calls(
          id, step_id, session_id, server_name, tool_name, status, duration_ms, raw_attrs_json, created_at, updated_at
        )
        VALUES (?, 'step-2', 'sess-2', ?, ?, 'ok', 50, '{}', ?, ?)
        """,
        [
            (
                "mcp-1",
                "docker run --rm -i ghcr.io/example/mcp-issue-tracker:latest",
                "jira_search",
                now,
                now,
            ),
            (
                "mcp-2",
                "npx mcp-remote https://metrics.example.test/mgmt/api/v1/mcp --header Authorization:${MCP_API_KEY} --verbose",
                "cx_dashboards",
                now,
                now,
            ),
        ],
    )
    conn.commit()


def test_build_overview_from_rollups_and_canonical_tables(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_view_db(conn)

        overview = build_overview(conn, limit=2)

        assert overview.session_count == 2
        assert overview.agent_count == 2
        assert overview.model_count == 2
        assert overview.tool_call_count == 3
        assert overview.input_tokens == 300
        assert overview.output_tokens == 90
        assert overview.estimated_cost_usd == 1.25
        assert overview.failure_count == 3
        assert overview.recovered_failure_count == 1
        assert overview.top_sessions[0]["session_id"] == "sess-2"
        assert overview.top_models[0]["model"] == "gpt-5.4"
        assert overview.top_tools[0]["tool_name"] == "Edit"
    finally:
        conn.close()


def test_list_sessions_paginates_and_filters_from_sql(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_view_db(conn)

        first_page = list_sessions(conn, limit=1)
        codex_page = list_sessions(conn, agent="codex")
        failed_page = list_sessions(conn, status="failed", min_failures=2)
        model_page = list_sessions(conn, model="claude-4.6-opus")
        cost_page = list_sessions(conn, min_cost=0.70, max_cost=1.00)

        assert first_page.total == 2
        assert first_page.limit == 1
        assert [row.session_id for row in first_page.rows] == ["sess-2"]
        assert [row.session_id for row in codex_page.rows] == ["sess-2"]
        assert [row.session_id for row in failed_page.rows] == ["sess-2"]
        assert [row.session_id for row in model_page.rows] == ["sess-1"]
        assert [row.session_id for row in cost_page.rows] == ["sess-2"]
        assert cost_page.rows[0].repo == "example/telemetry-app"
    finally:
        conn.close()


def test_build_report_tabs_view_models_from_sql(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        _seed_view_db(conn)

        tabs = build_report_tabs(conn)
        scoped = build_report_tabs(conn, session_ids={"sess-2"})

        assert tabs.activity.events_by_type == {"llm_call": 2}
        assert tabs.activity.activity_by_day == {"2026-05-01": 3, "2026-05-02": 5}
        assert tabs.models.models_by_count == {"claude-4.6-opus": 1, "gpt-5.4": 1}
        assert tabs.costs.model_costs["gpt-5.4"] == 0.75
        assert tabs.tools.tools_by_count == {"Edit": 2, "Read": 1}
        assert tabs.agents.agent_comparison[0]["name"] == "codex"
        assert tabs.graphs.graph_session_timeline

        assert scoped.tools.tools_by_count == {"Edit": 1}
        assert scoped.tools.top_commands == [{"command": "poetry run pytest", "count": 1}]
        assert scoped.mcp.mcp_servers_by_count == {"metrics.example.test": 1, "mcp-issue-tracker": 1}
        assert {node["type"] for node in scoped.graphs.graph_dep["nodes"]} >= {"agent", "tool", "mcp_tool", "mcp_server"}
        assert {
            (link["source"], link["target"])
            for link in scoped.graphs.graph_dep["links"]
        } >= {
            ("agent:codex", "mcp_tool:mcp-issue-tracker"),
            ("agent:codex", "mcp_tool:metrics.example.test"),
            ("mcp_tool:mcp-issue-tracker", "mcp_server:mcp-issue-tracker"),
            ("mcp_tool:metrics.example.test", "mcp_server:metrics.example.test"),
        }
    finally:
        conn.close()
