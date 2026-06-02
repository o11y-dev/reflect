from __future__ import annotations

from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite
from reflect.views.overview import build_overview
from reflect.views.report_tabs import _semantic_graph, build_report_tabs
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
        INSERT INTO steps(
          id, session_id, seq, type, started_at, status, summary,
          raw_attrs_json, created_at, updated_at
        )
        VALUES (?, ?, ?, 'llm_call', ?, 'completed', ?, ?, ?, ?)
        """,
        [
            ("step-1", "sess-1", 1, "2026-05-01T10:00:00+00:00", "", "{}", now, now),
            (
                "step-2",
                "sess-2",
                1,
                "2026-05-02T11:00:00+00:00",
                "UserPromptSubmit",
                '{"gen_ai.client.prompt":"Use /review-skill and the `research-helper` subagent"}',
                now,
                now,
            ),
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
        INSERT INTO steps(
          id, session_id, seq, type, started_at, status, summary,
          raw_attrs_json, created_at, updated_at
        )
        VALUES (?, 'sess-2', ?, 'tool_call', ?, 'completed', ?, ?, ?, ?)
        """,
        [
            (
                "step-copilot-task",
                2,
                "2026-05-02T11:01:00+00:00",
                "PreToolUse",
                '{"gen_ai.client.hook.event":"PreToolUse","gen_ai.client.name":"copilot","gen_ai.client.tool_name":"task","gen_ai.client.tool.input":"{\\"agent_type\\":\\"explore\\",\\"name\\":\\"repo-strategy\\"}"}',
                now,
                now,
            ),
            (
                "step-legacy-ide-subagent",
                3,
                "2026-05-02T11:02:00+00:00",
                "ide.hook.SubagentStart",
                '{"ide.hook.event":"SubagentStart","ide.name":"cursor","ide.subagent_type":"legacy-helper"}',
                now,
                now,
            ),
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
        INSERT INTO daily_rollups(
          day, agent, session_count, prompt_count, tool_call_count, error_count,
          input_tokens, output_tokens, total_cost, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("2026-05-01", "claude", 1, 1, 1, 1, 100, 40, 0.50, now),
            ("2026-05-02", "codex", 1, 1, 2, 2, 200, 50, 0.75, now),
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
    conn.execute(
        """
        INSERT INTO specs(id, repo_id, title, status, owner, source_path, created_at, updated_at)
        VALUES ('spec-1', 'repo-1', 'SQL report parity', 'active', 'team', 'docs/specs/sql.md', ?, ?)
        """,
        (now, now),
    )
    conn.executemany(
        """
        INSERT INTO requirements(
          id, spec_id, title, description, status, priority, evidence_status,
          confidence, created_at, updated_at
        )
        VALUES (?, 'spec-1', ?, '', ?, 'high', ?, ?, ?, ?)
        """,
        [
            ("req-1", "Render SQL tabs", "validated", "present", 0.9, now, now),
            ("req-2", "Export from SQL", "planned", "missing", 0.2, now, now),
        ],
    )
    conn.execute(
        """
        INSERT INTO evidence(
          id, requirement_id, session_id, repo_id, kind, summary, confidence,
          raw_json, created_at, updated_at
        )
        VALUES ('ev-1', 'req-1', 'sess-2', 'repo-1', 'test', 'SQL view test', 0.8, '{}', ?, ?)
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO memories(
          id, scope, type, repo_id, session_id, spec_id, content_hash,
          content_preview_redacted, confidence, sensitivity, source, last_seen_at,
          raw_attrs_json, created_at, updated_at
        )
        VALUES (
          'mem-1', 'repo', 'convention', 'repo-1', 'sess-2', 'spec-1', 'hash-1',
          'Use SQL view models for report tabs', 0.8, 'low', 'test',
          '2026-05-02T11:09:00+00:00', '{}', ?, ?
        )
        """,
        (now, now),
    )
    conn.execute(
        """
        INSERT INTO memories(
          id, scope, type, repo_id, session_id, content_hash,
          content_preview_redacted, confidence, sensitivity, source, last_seen_at,
          raw_attrs_json, created_at, updated_at
        )
        VALUES (
          'mem-cursor-plan', 'user', 'cursor_plan', 'repo-1', 'sess-2', 'hash-plan',
          'Cursor migration plan', 1.0, 'unknown', 'filesystem_instruction_scan',
          '2026-05-03T12:00:00+00:00',
          '{"path":"/workspace/.cursor/plans/migration.plan.md","name":"migration.plan.md"}', ?, ?
        )
        """,
        (now, now),
    )
    conn.executemany(
        """
        INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            ("gn-session", "Session", "sess-2", "sess-2", now, now, "{}", now, now),
            ("gn-memory", "Memory", "mem-1", "sess-2", now, now, '{"scope":"repo","type":"convention"}', now, now),
            ("gn-path", "Path", "AGENTS.md", None, now, now, '{"source":"filesystem_instruction_scan"}', now, now),
            (
                "gn-spec",
                "Spec",
                "SQL view test spec",
                None,
                now,
                now,
                '{"status":"active","source_path":"docs/specs/sql-view-test.md"}',
                now,
                now,
            ),
            (
                "gn-spec-path",
                "Path",
                "docs/specs/sql-view-test.md",
                None,
                now,
                now,
                '{"source":"spec"}',
                now,
                now,
            ),
        ],
    )
    conn.executemany(
        """
        INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES (?, 'Session', ?, ?, ?, ?, '{}', ?, ?)
        """,
        [
            (f"gn-extra-session-{index}", f"a-session-{index:03d}", f"a-session-{index:03d}", now, now, now, now)
            for index in range(420)
        ],
    )
    conn.execute(
        """
        INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, weight, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES ('ge-memory-path', 'gn-memory', 'gn-path', 'described_by_path', 'sess-2', 1, ?, ?, '{}', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, weight, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES ('ge-session-memory', 'gn-session', 'gn-memory', 'recorded_memory', 'sess-2', 1, ?, ?, '{}', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, weight, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES ('ge-session-spec', 'gn-session', 'gn-spec', 'addressed_spec', 'sess-2', 1, ?, ?, '{}', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, weight, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES ('ge-spec-path', 'gn-spec', 'gn-spec-path', 'described_by_path', NULL, 1, ?, ?, '{}', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.executemany(
        """
        INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?)
        """,
        [
            ("gn-global-memory", "Memory", "global-memory", now, now, '{"scope":"user"}', now, now),
            ("gn-global-path", "Path", "global.md", now, now, '{"source":"filesystem_instruction_scan"}', now, now),
        ],
    )
    conn.execute(
        """
        INSERT INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, weight, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
        VALUES ('ge-global-memory-path', 'gn-global-memory', 'gn-global-path', 'described_by_path', NULL, 1, ?, ?, '{}', ?, ?)
        """,
        (now, now, now, now),
    )
    conn.execute(
        """
        INSERT INTO privacy_findings(
          id, session_id, step_id, finding_type, severity, field_name,
          action_taken, detail_redacted, created_at
        )
        VALUES ('privacy-1', 'sess-2', 'step-2', 'token', 'medium', 'tool.input', 'redacted', 'example token', ?)
        """,
        (now,),
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
        assert isinstance(overview.source_provenance, list)
        assert overview.agent_cost_over_time[0]["agent"] == "claude"
        assert overview.agent_cost_over_time[1]["total_cost"] == 0.75
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

        assert tabs.activity.events_by_type == {"llm_call": 2, "tool_call": 2}
        assert tabs.activity.activity_by_day == {"2026-05-01": 3, "2026-05-02": 5}
        assert tabs.models.models_by_count == {"claude-4.6-opus": 1, "gpt-5.4": 1}
        assert tabs.costs.model_costs["gpt-5.4"] == 0.75
        assert tabs.tools.tools_by_count == {"Edit": 2, "Read": 1}
        assert tabs.agents.agent_comparison[0]["name"] == "codex"
        assert tabs.graphs.graph_session_timeline
        assert tabs.graphs.graph_semantic["nodes"]
        assert any(
            node["kind"] == "Session" and node["label"] == "sess-2"
            for node in tabs.graphs.graph_semantic["nodes"]
        )
        assert {"addressed_spec", "described_by_path"} <= {
            edge["kind"] for edge in tabs.graphs.graph_semantic["edges"]
        }
        assert any(node["kind"] == "Spec" and node["label"] == "SQL view test spec" for node in tabs.graphs.graph_semantic["nodes"])
        assert any(item["kind"] == "Spec" for item in tabs.graphs.graph_semantic["legend"])
        unscoped_node_ids = {node["id"] for node in tabs.graphs.graph_semantic["nodes"]}
        unscoped_edge_node_ids = {
            node_id
            for edge in tabs.graphs.graph_semantic["edges"]
            for node_id in (edge["source"], edge["target"])
        }
        assert unscoped_node_ids <= unscoped_edge_node_ids

        assert scoped.tools.tools_by_count == {"Edit": 1}
        assert scoped.tools.skills_by_count == {"review-skill": 1}
        assert scoped.tools.subagent_types_by_count == {
            "legacy-helper": 1,
            "repo-strategy": 1,
            "research-helper": 1,
        }
        assert scoped.tools.top_commands == [{"command": "poetry run pytest", "count": 1}]
        assert scoped.mcp.mcp_servers_by_count == {"metrics.example.test": 1, "mcp-issue-tracker": 1}
        assert scoped.agents.agents["codex"]["top_skills"] == {"review-skill": 1}
        assert scoped.agents.agents["codex"]["subagents"] == 1
        assert scoped.agents.agents["copilot"]["subagents"] == 1
        assert scoped.agents.agents["cursor"]["subagents"] == 1
        assert any(node["label"] == "AGENTS.md" for node in scoped.graphs.graph_semantic["nodes"])
        assert any(node["kind"] == "Spec" and node["label"] == "SQL view test spec" for node in scoped.graphs.graph_semantic["nodes"])
        assert all(node["label"] != "global-memory" for node in scoped.graphs.graph_semantic["nodes"])
        assert all(node["label"] != "global.md" for node in scoped.graphs.graph_semantic["nodes"])
        scoped_node_ids = {node["id"] for node in scoped.graphs.graph_semantic["nodes"]}
        scoped_edge_node_ids = {
            node_id
            for edge in scoped.graphs.graph_semantic["edges"]
            for node_id in (edge["source"], edge["target"])
        }
        assert scoped_node_ids <= scoped_edge_node_ids
        assert scoped.specs.total_specs == 2
        assert scoped.specs.specs[0]["title"] == "migration"
        assert scoped.specs.specs[0]["status"] == "plan"
        assert scoped.specs.requirements_by_status == {"planned": 1, "validated": 1}
        assert scoped.memory.memories_by_type == {"convention": 1}
        assert all(memory["type"] != "cursor_plan" for memory in scoped.memory.recent_memories)
        assert scoped.privacy.findings_by_severity == {"medium": 1}
        assert scoped.exports.row_counts["memories"] == 2
        assert scoped.exports.row_counts["privacy_findings"] == 1
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


def test_semantic_graph_keeps_memory_bridges_with_hot_edge_budget(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        now = "2026-05-03T00:00:00+00:00"
        conn.executemany(
            """
            INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
            VALUES (?, 'Session', ?, ?, ?, ?, '{}', ?, ?)
            """,
            [
                (f"session-node-{idx}", f"sess-hot-{idx}", f"sess-hot-{idx}", now, now, now, now)
                for idx in range(40)
            ],
        )
        conn.execute(
            """
            INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
            VALUES ('memory-node-1', 'Memory', 'instruction-1', NULL, ?, ?, '{"scope":"project"}', ?, ?)
            """,
            (now, now, now, now),
        )
        conn.execute(
            """
            INSERT INTO graph_nodes(id, kind, label, session_id, first_seen_at, last_seen_at, attrs_json, created_at, updated_at)
            VALUES ('path-node-1', 'Path', 'AGENTS.md', NULL, ?, ?, '{"source":"filesystem_instruction_scan"}', ?, ?)
            """,
            (now, now, now, now),
        )
        heavy_edges = []
        for idx in range(950):
            source = f"session-node-{idx % 40}"
            target = f"session-node-{(idx * 7 + 3) % 40}"
            heavy_edges.append(
                (
                    f"hot-edge-{idx}",
                    source,
                    target,
                    "has_step",
                    f"sess-hot-{idx % 40}",
                    10,
                    now,
                    now,
                    "{}",
                    now,
                    now,
                )
            )
        conn.executemany(
            """
            INSERT INTO graph_edges(
              id, source_node_id, target_node_id, kind, session_id, weight,
              first_seen_at, last_seen_at, attrs_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            heavy_edges,
        )
        conn.execute(
            """
            INSERT INTO graph_edges(
              id, source_node_id, target_node_id, kind, session_id, weight,
              first_seen_at, last_seen_at, attrs_json, created_at, updated_at
            )
            VALUES (
              'memory-edge-1', 'memory-node-1', 'path-node-1', 'described_by_path', NULL, 1,
              ?, ?, '{}', ?, ?
            )
            """,
            (now, now, now, now),
        )
        conn.commit()

        graph = _semantic_graph(conn, None)

        node_ids = {node["id"] for node in graph["nodes"]}
        assert "memory-node-1" in node_ids
        assert "path-node-1" in node_ids
        assert any(
            edge["kind"] == "described_by_path"
            and {edge["source"], edge["target"]} == {"memory-node-1", "path-node-1"}
            for edge in graph["edges"]
        )
    finally:
        conn.close()
