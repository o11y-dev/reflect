from __future__ import annotations

import threading
from collections import Counter

from fastapi.testclient import TestClient

from reflect.dashboard import _build_dashboard_app
from reflect.improvements.models import (
    EvidenceRef,
    ObservationDraft,
    RuleDefinition,
    Severity,
    WorkflowProposal,
)
from reflect.improvements.repository import ImprovementRepository
from reflect.improvements.skills import SkillRegistryService
from reflect.models import TelemetryStats
from reflect.preparation import BackgroundPreparationWorker
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _stats() -> TelemetryStats:
    return TelemetryStats(
        session_files=0,
        span_files=0,
        total_events=0,
        events_by_type=Counter(),
        events_by_file={},
        sessions_seen=set(),
        session_events={},
        session_models={},
        session_first_ts={},
        agents={},
        session_tokens={},
    )


def _seed_sql_report_db(db_path):
    now = "2026-05-01T09:00:00+00:00"
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, version, created_at, updated_at)
            VALUES ('agent-codex', 'codex', 'cli', 'test', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, ended_at, status, title, input_tokens,
              output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'sess-sql', 'agent-codex', '2026-05-01T10:00:00+00:00',
              '2026-05-01T10:02:00+00:00', 'completed', 'SQL session',
              120, 30, 0.42, ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'step-sql', 'sess-sql', 1, 'llm_call',
              '2026-05-01T10:00:00+00:00', 'completed',
              'gen_ai.client.hook.UserPromptSubmit',
              '{"gen_ai.client.prompt":"Fix the failing SQL dashboard tests with /review-skill and the `research-helper` subagent","gen_ai.client.generation_id":"gen-1"}',
              ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'response-step-sql', 'sess-sql', 2, 'llm_call',
              '2026-05-01T10:00:30+00:00', 'completed',
              'gen_ai.client.hook.Stop',
              '{"gen_ai.client.status":"completed","gen_ai.client.generation_id":"gen-1"}',
              ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, duration_ms, status,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'tool-step-sql', 'sess-sql', 3, 'tool_call',
              '2026-05-01T10:01:00+00:00', 500, 'ok',
              '{"gen_ai.client.tool.input.file_path":"/Users/test/.cursor/skills/sql-review/SKILL.md"}',
              ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, cache_creation_input_tokens,
              cache_read_input_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES ('llm-sql', 'response-step-sql', 'sess-sql', 'openai', 'gpt-5.4', 'gpt-5.4', 120, 30, 10, 90, 0.42, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO session_rollups(
              session_id, agent, started_at, ended_at, duration_ms, prompt_count,
              tool_call_count, error_count, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, total_cost, updated_at
            )
            VALUES (
              'sess-sql', 'codex', '2026-05-01T10:00:00+00:00',
              '2026-05-01T10:02:00+00:00', 120000, 1, 2, 0,
              120, 30, 90, 10, 0.42, ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO daily_rollups(
              day, agent, session_count, prompt_count, tool_call_count, error_count,
              input_tokens, output_tokens, total_cost, updated_at
            )
            VALUES ('2026-05-01', 'codex', 1, 1, 2, 0, 120, 30, 0.42, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tool_rollups(
              tool_name, agent, call_count, success_count, error_count, total_duration_ms, updated_at
            )
            VALUES ('exec_command', 'codex', 2, 2, 0, 500, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO tool_calls(
              id, step_id, session_id, tool_name, tool_type, input_preview_redacted,
              status, duration_ms, raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'tool-sql', 'tool-step-sql', 'sess-sql', 'exec_command', 'shell',
              '{"cmd":"poetry run pytest"}', 'ok', 500,
              '{"gen_ai.client.tool.input.file_path":"/Users/test/.cursor/skills/sql-review/SKILL.md"}',
              ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO mcp_calls(
              id, step_id, session_id, server_name, tool_name, status,
              duration_ms, raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'mcp-sql', 'tool-step-sql', 'sess-sql',
              'docker run --rm -i -e TRACKER_API_TOKEN=secret ghcr.io/example/mcp-issue-tracker:latest',
              'jira_search', 'ok', 200, '{}', ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
              session_id, observed_at, received_at, attrs_json, body_json,
              normalized_status, content_hash, created_at
            )
            VALUES (
              'raw-trace-prompt-sql', 'otel-traces.json', 'otlp_traces_json',
              'gen_ai.client.hook.UserPromptSubmit', 'trace-sql', 'span-prompt', '',
              'sess-sql', '2026-05-01T10:00:00+00:00',
              '2026-05-01T10:00:00+00:00',
              '{}', '{}',
              'ok', 'raw-trace-prompt-sql-hash', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
              session_id, observed_at, received_at, attrs_json, body_json,
              normalized_status, content_hash, created_at
            )
            VALUES (
              'raw-trace-tool-sql', 'otel-traces.json', 'otlp_traces_json',
              'tool_call', 'trace-sql', 'span-tool', 'span-prompt',
              'sess-sql', '2026-05-01T10:01:00+00:00',
              '2026-05-01T10:01:00+00:00',
              '{}', '{}',
              'ok', 'raw-trace-tool-sql-hash', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO raw_events(
              id, source_id, source_type, event_type, trace_id, span_id, parent_span_id,
              session_id, observed_at, received_at, attrs_json, body_json,
              normalized_status, content_hash, created_at
            )
            VALUES (
              'raw-log-sql', 'otel-logs.json', 'otlp_logs_json',
              'gen_ai.client.hook.UserPromptSubmit', '', '', '',
              'sess-sql', '2026-05-01T10:00:05+00:00',
              '2026-05-01T10:00:05+00:00',
              '{"service.name":"claude-code","gen_ai.client.name":"claude","gen_ai.client.hook.event":"UserPromptSubmit"}',
              '{"message":"User prompt submitted"}',
              'ok', 'raw-log-sql-hash', ?
            )
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO specs(id, title, status, owner, source_path, created_at, updated_at)
            VALUES ('spec-sql', 'SQL report parity', 'active', 'team', 'docs/specs/sql.md', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO requirements(
              id, spec_id, title, description, status, priority, evidence_status,
              confidence, created_at, updated_at
            )
            VALUES ('req-sql', 'spec-sql', 'Render SQL tabs', '', 'validated', 'high', 'present', 0.9, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO evidence(
              id, requirement_id, session_id, kind, summary, confidence,
              raw_json, created_at, updated_at
            )
            VALUES ('ev-sql', 'req-sql', 'sess-sql', 'test', 'SQL view test', 0.8, '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO memories(
              id, scope, type, session_id, spec_id, content_hash,
              content_preview_redacted, confidence, sensitivity, source, last_seen_at,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'mem-sql', 'repo', 'convention', 'sess-sql', 'spec-sql', 'hash-sql',
              'Use SQL view models for report tabs', 0.8, 'low', 'test',
              '2026-05-01T10:01:00+00:00', '{}', ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO privacy_findings(
              id, session_id, step_id, finding_type, severity, field_name,
              action_taken, detail_redacted, created_at
            )
            VALUES ('privacy-sql', 'sess-sql', 'tool-step-sql', 'token', 'medium', 'tool.input', 'redacted', 'example token', ?)
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_improvement_ledger(db_path):
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        repository = ImprovementRepository(conn)
        rule = RuleDefinition(
            id="test_rule",
            version=1,
            category="verification",
            title="Test rule",
            description="Deterministic test rule",
        )
        repository.sync_rule_definitions((rule,), now="2026-05-01T11:00:00+00:00")
        observation_id = repository.upsert_observation(
            ObservationDraft(
                rule_id=rule.id,
                rule_version=rule.version,
                scope_type="user",
                scope_id="local",
                fingerprint="test-fingerprint",
                category=rule.category,
                title="Verification is missing",
                summary="One test session changed code without verification.",
                metric_name="unverified_change_sessions",
                metric_value=1,
                metric_unit="sessions",
                metric_direction="lower_is_better",
                impact_score=60,
                severity=Severity.MEDIUM,
                confidence=0.8,
                evidence=[
                    EvidenceRef(
                        entity_type="session",
                        entity_id="sess-sql",
                        session_id="sess-sql",
                        summary_redacted="Test session evidence",
                    )
                ],
            ),
            now="2026-05-01T11:00:00+00:00",
        )
        repository.ensure_candidate(
            observation_id,
            proposal=WorkflowProposal(
                title="Workflow: Verification is missing",
                hypothesis="A reviewed verification workflow will reduce missing verification.",
                risk="low",
                content={
                    "schema_version": 1,
                    "slug": "verify-before-done",
                    "behavior_type": "verification",
                    "description": "Verify changes before completion.",
                    "steps": ["Run the focused test."],
                    "source": {"rule_id": rule.id},
                },
                target_metric="unverified_change_sessions",
                target_value=0.7,
            ),
            now="2026-05-01T11:00:00+00:00",
        )
        SkillRegistryService(conn).sync_workflow_candidates()
        conn.commit()
        return observation_id
    finally:
        conn.close()


def _add_sql_baseline_session(db_path):
    now = "2026-05-01T12:00:00+00:00"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO agents(id, name, kind, version, created_at, updated_at)
            VALUES ('agent-claude', 'claude', 'cli', 'test', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, ended_at, status, title, input_tokens,
              output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'sess-baseline', 'agent-claude', '2026-04-30T10:00:00+00:00',
              '2026-04-30T10:03:00+00:00', 'completed', 'Baseline session',
              80, 20, 0.21, ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(id, session_id, seq, type, started_at, status, summary, raw_attrs_json, created_at, updated_at)
            VALUES ('baseline-step', 'sess-baseline', 1, 'llm_call', '2026-04-30T10:00:00+00:00', 'completed', '', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES ('baseline-llm', 'baseline-step', 'sess-baseline', 'anthropic', 'gpt-5.4', 'gpt-5.4', 80, 20, 0.21, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO session_rollups(
              session_id, agent, started_at, ended_at, duration_ms, prompt_count,
              tool_call_count, error_count, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, total_cost, updated_at
            )
            VALUES (
              'sess-baseline', 'claude', '2026-04-30T10:00:00+00:00',
              '2026-04-30T10:03:00+00:00', 180000, 1, 1, 1,
              80, 20, 0, 0, 0.21, ?
            )
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def _add_sql_codex_sibling_session(db_path):
    now = "2026-05-01T12:30:00+00:00"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO sessions(
              id, agent_id, started_at, ended_at, status, title, input_tokens,
              output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'sess-codex-2', 'agent-codex', '2026-05-01T11:00:00+00:00',
              '2026-05-01T11:03:00+00:00', 'completed', 'Second Codex session',
              60, 15, 0.11, ?, ?
            )
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO steps(id, session_id, seq, type, started_at, status, summary, raw_attrs_json, created_at, updated_at)
            VALUES ('codex-2-step', 'sess-codex-2', 1, 'llm_call', '2026-05-01T11:00:00+00:00', 'completed', '', '{}', ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES ('codex-2-llm', 'codex-2-step', 'sess-codex-2', 'openai', 'gpt-5.4', 'gpt-5.4', 60, 15, 0.11, ?, ?)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO session_rollups(
              session_id, agent, started_at, ended_at, duration_ms, prompt_count,
              tool_call_count, error_count, input_tokens, output_tokens,
              cache_read_tokens, cache_write_tokens, total_cost, updated_at
            )
            VALUES (
              'sess-codex-2', 'codex', '2026-05-01T11:00:00+00:00',
              '2026-05-01T11:03:00+00:00', 180000, 1, 1, 0,
              60, 15, 0, 0, 0.11, ?
            )
            """,
            (now,),
        )
        conn.commit()
    finally:
        conn.close()


def test_dashboard_api_embeds_sql_view_models(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data")

    assert response.status_code == 200
    sqlite_payload = response.json()["sqlite"]
    assert sqlite_payload["overview"]["session_count"] == 1
    assert sqlite_payload["overview"]["agent_cost_over_time"][0]["total_cost"] == 0.42
    assert sqlite_payload["overview"]["top_tools"][0]["tool_name"] == "exec_command"
    assert sqlite_payload["sessions"]["rows"][0]["session_id"] == "sess-sql"
    assert sqlite_payload["tabs"]["specs"]["total_specs"] == 1
    assert sqlite_payload["tabs"]["memory"]["total_memories"] == 1
    assert sqlite_payload["tabs"]["privacy"]["total_findings"] == 1
    assert sqlite_payload["tabs"]["exports"]["row_counts"]["sessions"] == 1


def test_dashboard_api_uses_sql_when_db_is_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)

    def _raise_legacy_json(_stats):
        raise AssertionError("legacy dashboard JSON should not be built")

    monkeypatch.setattr("reflect.dashboard._build_dashboard_json", _raise_legacy_json)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sql_backed"] is True
    assert "sql_only" not in payload
    assert payload["sqlite"]["overview"]["session_count"] == 1
    assert payload["sqlite"]["tabs"]["specs"]["requirements_by_status"] == {"validated": 1}
    assert payload["sqlite"]["tabs"]["exports"]["scoped"] is False
    assert payload["sqlite"]["tabs"]["tools"]["skills_by_count"] == {"review-skill": 1, "sql-review": 1}
    assert payload["sqlite"]["tabs"]["agents"]["agents"]["codex"]["top_skills"] == {"review-skill": 1, "sql-review": 1}
    assert payload["sqlite"]["tabs"]["overview"]["unique_sessions"] == payload["unique_sessions"]
    assert payload["sqlite"]["tabs"]["overview"]["prompt_submits"] == payload["prompt_submits"]
    assert payload["sqlite"]["tabs"]["overview"]["tool_calls"] == payload["tool_calls"]
    assert payload["sqlite"]["tabs"]["overview"]["models_by_count"] == payload["models_by_count"]
    assert payload["sqlite"]["tabs"]["overview"]["events_by_type"] == payload["events_by_type"]
    assert payload["sqlite"]["tabs"]["overview"]["model_costs"] == payload["model_costs"]
    assert payload["sessions"][0]["id"] == "sess-sql"
    assert payload["sessions"][0]["first_prompt"].startswith("Fix the failing SQL dashboard tests")
    assert payload["sessions"][0]["duration_ms"] == 120000
    assert payload["sessions"][0]["quality_score"] > 0
    assert payload["avg_quality_score"] > 0
    assert payload["activity_by_day"]["2026-05-01"] == 3
    assert payload["activity_by_hour"]["10"] == 3
    assert payload["events_by_type"] == {"llm_call": 2, "tool_call": 1}
    assert payload["models_by_count"] == {"gpt-5.4": 1}
    assert payload["model_costs"]["gpt-5.4"] == 0.42
    assert payload["total_cost_usd"] == 0.42
    assert payload["input_cost_usd"] > 0
    assert payload["output_cost_usd"] > 0
    assert payload["cache_read_cost_usd"] > 0
    assert payload["tools_by_count"] == {"exec_command": 2}
    assert payload["mcp_servers_by_count"] == {"mcp-issue-tracker": 1}
    assert "docker run" not in next(iter(payload["mcp_servers_by_count"]))
    assert payload["skills_by_count"] == {"review-skill": 1, "sql-review": 1}
    assert payload["subagent_types_by_count"] == {"research-helper": 1}
    assert payload["top_commands"] == [{"command": "poetry run pytest", "count": 1}]
    assert payload["unique_commands"] == 1
    assert payload["shell_executions"] == 1
    assert payload["tool_percentiles"][0]["tool"] == "exec_command"
    assert payload["agent_comparison"][0]["name"] == "codex"
    assert payload["strengths"]
    assert payload["observations"]
    assert payload["recommendations"]
    assert payload["sqlite"]["tabs"]["observations"]["strengths"] == payload["strengths"]
    assert payload["sqlite"]["tabs"]["observations"]["observations"] == payload["observations"]
    assert payload["sqlite"]["tabs"]["observations"]["recommendations"] == payload["recommendations"]
    assert payload["sqlite"]["tabs"]["observations"]["token_economy"] == payload["token_economy"]
    assert any("Reduce MCP context bloat" in rec for rec in payload["recommendations"])
    assert all("SQL view models" not in rec for rec in payload["recommendations"])
    assert payload["pricing_source"] == "local"
    assert all("SQL" not in item and "SQLite" not in item for item in payload["strengths"])
    assert all("SQL" not in item and "SQLite" not in item for item in payload["observations"])
    assert all("SQL has" not in item and "SQL-observed" not in item for item in payload["recommendations"])
    assert all(achievement["name"] != "SQL Report Store" for achievement in payload["achievements"])
    assert payload["practical_examples"]
    assert len(payload["achievements"]) >= 5
    assert payload["total_cache_creation_tokens"] == 10
    assert payload["total_cache_read_tokens"] == 90
    assert payload["token_economy"]["total_tokens"] == 250
    assert payload["token_economy"]["cache_hit_pct"] == 75
    assert payload["graph_dep"]["nodes"]
    assert {node["type"] for node in payload["graph_dep"]["nodes"]} >= {"agent", "mcp_tool", "mcp_server"}
    assert payload["graph_session_timeline"][0]["spans"][0]["tool"] == "exec_command"

    detail = TestClient(app).get("/api/session/sess-sql")
    assert detail.status_code == 200
    conversation = detail.json()["conversation"]
    assert [event["type"] for event in conversation[:2]] == ["prompt", "response"]
    assert conversation[0]["preview"].startswith("Fix the failing SQL dashboard tests")
    assert "Assistant turn completed" in conversation[1]["preview"]
    assert detail.json()["telemetry"]["summary"]["spans"] == 3
    spans = detail.json()["telemetry"]["spans"]
    prompt_span = next(span for span in spans if span["id"] == "step-sql")
    tool_span = next(span for span in spans if span["id"] == "tool-step-sql")
    assert prompt_span["trace_id"] == "trace-sql"
    assert tool_span["parent_span_id"] == "span-prompt"
    assert tool_span["parent_id"] == "step-sql"
    assert detail.json()["telemetry"]["summary"]["logs"] == 1
    assert detail.json()["telemetry"]["logs"][0]["event"] == "UserPromptSubmit"
    assert "User prompt submitted" in detail.json()["telemetry"]["logs"][0]["body"]
    tool_inventory = detail.json()["tool_inventory"]
    assert tool_inventory["tools"][0]["name"] == "exec_command"
    assert tool_inventory["tools"][0]["count"] == 1
    assert {skill["name"] for skill in tool_inventory["skills"]} >= {"review-skill", "sql-review"}
    assert {subagent["name"] for subagent in tool_inventory["subagents"]} >= {"research-helper"}
    assert tool_inventory["mcp_tools"][0]["name"].endswith("/jira_search")


def test_dashboard_improvement_endpoints_expose_durable_ledger(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    observation_id = _seed_improvement_ledger(db_path)
    project_root = tmp_path / "project"
    (project_root / ".git").mkdir(parents=True)
    app = _build_dashboard_app(
        _stats(),
        docs_dir=tmp_path,
        db_path=db_path,
        project_root=project_root,
    )
    client = TestClient(app)

    inbox = client.get("/api/inbox")
    detail = client.get(f"/api/inbox/{observation_id}")
    legacy_inbox = client.get("/api/improvements")
    legacy_detail = client.get(f"/api/improvements/{observation_id}")
    workflows = client.get("/api/workflows")
    verification_workflows = client.get("/api/workflows?type=verification&status=pending")
    loop_workflows = client.get("/api/workflows?type=loop")
    loops = client.get("/api/loops")
    skills = client.get("/api/skills")
    measurements = client.get("/api/impact")
    missing_measurement_sessions = client.get("/api/impact/missing/sessions")
    legacy_measurements = client.get("/api/measurements")
    rules = client.get("/api/rules")

    assert inbox.status_code == 200
    assert inbox.json()["findings"][0]["id"] == observation_id
    assert inbox.json()["inbox_total_count"] == 1
    assert inbox.json()["raw_observation_count"] == 1
    assert detail.status_code == 200
    assert detail.json()["evidence"][0]["session_id"] == "sess-sql"
    assert legacy_inbox.json()["observations"][0]["id"] == observation_id
    assert legacy_detail.json() == detail.json()
    assert workflows.status_code == 200
    assert workflows.json()["workflows"][0]["status"] == "pending"
    assert workflows.json()["workflows"][0]["skill_id"].startswith("skill_")
    assert verification_workflows.status_code == 200
    assert verification_workflows.json()["workflows"][0]["content"]["behavior_type"] == "verification"
    assert loop_workflows.json() == {"workflows": []}
    assert loops.status_code == 200
    assert loops.json()["loops"] == []
    assert skills.status_code == 200
    assert skills.json()["skills"][0]["slug"] == "verify-before-done"
    assert skills.json()["skills"][0]["installation_targets"] == []
    assert skills.json()["total_count"] == len(skills.json()["skills"])
    assert all(
        item["lifecycle_state"] != "stale"
        for item in skills.json()["skills"]
    )
    assert "archived_count" in skills.json()
    assert "counts_by_lifecycle" in skills.json()
    skill_id = skills.json()["skills"][0]["id"]
    skill_detail = client.get(f"/api/skills/{skill_id}")
    assert skill_detail.status_code == 200
    assert skill_detail.json()["versions"][0]["status"] == "pending"
    assert skill_detail.json()["usage_sessions"] == []
    assert measurements.status_code == 200
    assert measurements.json() == {"impact_checks": []}
    assert legacy_measurements.json() == {"measurements": []}
    assert missing_measurement_sessions.status_code == 404
    assert "Measurement not found" in missing_measurement_sessions.json()["error"]
    assert rules.status_code == 200
    assert rules.json()["rules"][0]["id"] == "test_rule"
    assert rules.json()["rules"][0]["open_observation_count"] == 1
    assert rules.json()["extension"]["kind"] == "code_backed"
    assert rules.json()["extension"]["base_class"] == "BaseImprovementRule"
    assert rules.json()["extension"]["registry"] == "DEFAULT_RULE_REGISTRY"

    workflow = workflows.json()["workflows"][0]
    candidate_id = workflow["id"]
    preview = client.get(f"/api/workflows/{candidate_id}/preview")
    sessions = client.get(f"/api/workflows/{candidate_id}/sessions")
    assert preview.status_code == 200
    assert preview.json()["would_change"] is True
    assert preview.json()["diff"].startswith("--- ")
    assert preview.json()["change_kind"] == "create"
    assert preview.json()["checks"]["apply_allowed"] is True
    assert preview.json()["application_repository"] == str(project_root)
    assert preview.json()["target_relative_path"] == ".agents/skills/verify-before-done/SKILL.md"
    assert sessions.status_code == 200
    assert sessions.json()["source_session_count"] == 1
    assert sessions.json()["source_sessions"][0]["session_id"] == "sess-sql"

    unsafe_root = tmp_path / "not-a-repository"
    unsafe_root.mkdir()
    unsafe_preview = client.get(
        f"/api/workflows/{candidate_id}/preview",
        params={"project_root": str(unsafe_root)},
    )
    unsafe_apply = client.post(
        f"/api/workflows/{candidate_id}/apply",
        json={"project_root": str(unsafe_root)},
    )
    assert unsafe_preview.status_code == 200
    assert unsafe_preview.json()["checks"]["apply_allowed"] is False
    assert unsafe_apply.status_code == 409
    assert "containing .git" in unsafe_apply.json()["error"]

    edited_content = {
        **workflow["content"],
        "steps": ["Inspect exact evidence.", "Run focused verification."],
    }
    edited = client.put(
        f"/api/workflows/{candidate_id}",
        json={"content": edited_content},
    )
    assert edited.status_code == 200
    assert edited.json()["content"]["steps"] == edited_content["steps"]

    applied = client.post(f"/api/workflows/{candidate_id}/apply")
    applied_again = client.post(f"/api/workflows/{candidate_id}/apply")
    assert applied.status_code == 200
    assert applied.json()["idempotent"] is False
    assert applied_again.json()["idempotent"] is True
    assert applied_again.json()["intervention_id"] == applied.json()["intervention_id"]

    feedback = client.post(
        "/api/feedback/sess-sql",
        json={"outcome": "corrected", "reason": "Missed the requested verification"},
    )
    invalid_feedback = client.post(
        "/api/feedback/sess-sql",
        json={"outcome": "invented"},
    )
    assert feedback.status_code == 201
    assert feedback.json()["outcome"] == "corrected"
    assert invalid_feedback.status_code == 422

    rolled_back = client.post(f"/api/workflows/{candidate_id}/rollback")
    rejected = client.post(
        f"/api/workflows/{candidate_id}/reject",
        json={"reason": "Not appropriate for this repository"},
    )
    assert rolled_back.status_code == 200
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"


def test_dashboard_improvement_get_endpoints_are_read_only(tmp_path, monkeypatch):
    from reflect.improvements.loops import LoopService

    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    _seed_improvement_ledger(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    def reject_refresh(*_args, **_kwargs):
        raise AssertionError("dashboard GET endpoint attempted a registry refresh")

    monkeypatch.setattr(SkillRegistryService, "refresh", reject_refresh)
    monkeypatch.setattr(SkillRegistryService, "sync_workflow_candidates", reject_refresh)
    monkeypatch.setattr(LoopService, "refresh", reject_refresh)

    client = TestClient(app)
    workflows = client.get("/api/workflows")
    loops = client.get("/api/loops")
    skills = client.get("/api/skills")
    skill_id = skills.json()["skills"][0]["id"]
    skill_detail = client.get(f"/api/skills/{skill_id}")

    assert workflows.status_code == 200
    assert loops.status_code == 200
    assert skills.status_code == 200
    assert skill_detail.status_code == 200


def test_dashboard_api_reports_background_preparation_status(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    worker = BackgroundPreparationWorker(lambda: {"refreshed_sessions": 1})
    app = _build_dashboard_app(
        _stats(),
        docs_dir=tmp_path,
        db_path=db_path,
        preparation_worker=worker,
    )
    client = TestClient(app)

    assert client.get("/api/status").json()["preparation"]["state"] == "idle"
    assert worker.start() is True
    assert worker.wait(timeout=2) is True

    status = client.get("/api/status").json()["preparation"]
    assert status["state"] == "complete"
    assert status["generation"] == 1
    assert status["result"] == {"refreshed_sessions": 1}


def test_dashboard_minimal_snapshot_skips_all_tab_builders(tmp_path, monkeypatch):
    from reflect.dashboard import _sql_dashboard_payload

    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)

    def fail_if_built(*_args, **_kwargs):
        raise AssertionError("minimal snapshot should not build report tabs")

    monkeypatch.setattr("reflect.views.report_tabs.build_report_tabs", fail_if_built)
    monkeypatch.setattr("reflect.views.report_tabs.build_report_tab", fail_if_built)

    payload = _sql_dashboard_payload(db_path, lazy_all_tabs=True)

    assert payload["sessions"][0]["id"] == "sess-sql"
    assert payload["unique_sessions"] == 1
    assert payload["graph_semantic"] == {
        "nodes": [],
        "edges": [],
        "sessions": [],
        "legend": [],
    }


def test_dashboard_api_serves_current_snapshot_during_background_preparation(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    started = threading.Event()
    release = threading.Event()

    def prepare():
        started.set()
        release.wait(timeout=2)
        return {"refreshed_sessions": 1}

    worker = BackgroundPreparationWorker(prepare)
    app = _build_dashboard_app(
        _stats(),
        docs_dir=tmp_path,
        db_path=db_path,
        preparation_worker=worker,
    )
    client = TestClient(app)

    assert worker.start() is True
    assert started.wait(timeout=1) is True
    response = client.get("/api/data")

    assert response.status_code == 200
    assert response.json()["sessions"][0]["id"] == "sess-sql"
    assert client.get("/api/status").json()["preparation"]["state"] == "running"
    release.set()
    assert worker.wait(timeout=2) is True


def test_dashboard_filtered_bootstrap_skips_heavy_tabs(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    original = __import__("reflect.dashboard", fromlist=["_sql_dashboard_payload"])._sql_dashboard_payload
    calls = []

    def capture_payload(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr("reflect.dashboard._sql_dashboard_payload", capture_payload)

    response = TestClient(app).get("/api/data", params={"agents": "codex"})

    assert response.status_code == 200
    assert calls[-1]["lazy_heavy_tabs"] is True
    assert calls[-1]["include_comparison"] is False
    assert calls[-1]["base_tab_names"] == set()
    assert response.json()["graph_tool_transitions"] == []
    assert response.json()["comparison"] is None

    def reject_full_tools_tab(*_args, **_kwargs):
        raise AssertionError("Usage bootstrap must not build full tool percentiles and inventories")

    monkeypatch.setattr("reflect.views.report_tabs._build_tools", reject_full_tools_tab)
    calls.clear()
    usage = TestClient(app).get(
        "/api/data",
        params={"agents": "codex", "tab": "explore", "view": "usage"},
    )

    assert usage.status_code == 200
    assert calls[-1]["base_tab_names"] == {
        "activity", "models", "costs", "usage_tools", "mcp",
    }
    assert calls[-1]["include_comparison"] is True
    payload = usage.json()
    assert payload["shell_executions"] == 1
    assert payload["subagent_launches"] == 1
    assert payload["subagent_types_by_count"] == {"research-helper": 1}
    assert payload["source_provenance"]
    assert payload["agent_cost_over_time"] == [
        {"day": "2026-05-01", "agent": "codex", "total_cost": 0.42},
    ]
    assert len(payload["weekly_trends"]) == 1
    assert payload["failure_rate_pct"] == 0
    assert payload["sqlite"]["tabs"]["usage"]["shell_executions"] == 1


def test_dashboard_session_filter_uses_focused_fast_path(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    _add_sql_codex_sibling_session(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    def _raise_broad_payload(*args, **kwargs):
        raise AssertionError("session-only filter should not build the broad dashboard payload")

    monkeypatch.setattr("reflect.dashboard._sql_dashboard_payload", _raise_broad_payload)
    monkeypatch.setattr("reflect.dashboard._sql_dashboard_compat_payload", _raise_broad_payload)

    response = TestClient(app).get("/api/data?session=sess-sql")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sql_backed"] is True
    assert payload["focused_session_id"] == "sess-sql"
    assert payload["unique_sessions"] == 1
    assert payload["session_list_total"] == 2
    assert {session["id"] for session in payload["sessions"]} == {"sess-sql", "sess-codex-2"}
    selected_session = next(session for session in payload["sessions"] if session["id"] == "sess-sql")
    assert selected_session["first_prompt"].startswith("Fix the failing SQL dashboard tests")
    assert selected_session["primary_model"] == "gpt-5.4"
    assert selected_session["skills"] == {"review-skill": 1, "sql-review": 1}
    assert payload["tools_by_count"] == {"exec_command": 1}
    assert payload["skills_by_count"] == {"review-skill": 1, "sql-review": 1}
    assert payload["subagent_types_by_count"] == {"research-helper": 1}
    assert payload["mcp_servers_by_count"] == {"mcp-issue-tracker": 1}
    assert "docker run" not in next(iter(payload["mcp_servers_by_count"]))
    assert payload["sqlite"]["tabs"]["tools"]["skills_by_count"] == payload["skills_by_count"]
    assert payload["sqlite"]["tabs"]["overview"]["unique_sessions"] == 1
    assert payload["sqlite"]["tabs"]["activity"]["events_by_type"] == payload["events_by_type"]
    assert payload["sqlite"]["tabs"]["models"]["models_by_count"] == {"gpt-5.4": 1}
    assert payload["sqlite"]["tabs"]["agents"]["agent_comparison"][0]["sessions"] == 1
    assert payload["graph_semantic"] == {"nodes": [], "edges": [], "sessions": [], "legend": []}
    assert {row["session_id"] for row in payload["sqlite"]["sessions"]["rows"]} == {
        "sess-sql",
        "sess-codex-2",
    }
    assert payload["sqlite"]["tabs"]["graphs"]["graph_semantic"] == {
        "nodes": [],
        "edges": [],
        "sessions": [],
        "legend": [],
    }
    assert payload["sqlite"]["tabs"]["exports"]["scoped"] is True


def test_dashboard_lazy_tab_endpoint_builds_only_requested_scoped_tab(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    def _raise_all_tabs(*args, **kwargs):
        raise AssertionError("lazy tab endpoint should not build every report tab")

    monkeypatch.setattr("reflect.views.report_tabs.build_report_tabs", _raise_all_tabs)

    response = TestClient(app).get("/api/tabs/tools?session=sess-sql")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sql_backed"] is True
    assert payload["tab"] == "tools"
    assert payload["scoped"] is True
    assert payload["session_id"] == "sess-sql"
    assert payload["tools_by_count"] == {"exec_command": 1}
    assert payload["skills_by_count"] == {"review-skill": 1, "sql-review": 1}
    assert payload["subagent_types_by_count"] == {"research-helper": 1}


def test_dashboard_lazy_tab_endpoint_rejects_unknown_tab(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/tabs/not-a-tab?session=sess-sql")

    assert response.status_code == 404
    assert "Unknown report tab" in response.json()["error"]


def test_dashboard_explore_api_uses_product_view_names(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)
    client = TestClient(app)

    usage = client.get("/api/explore/usage?session=sess-sql")
    tools = client.get("/api/explore/tools?session=sess-sql")
    legacy_usage_name = client.get("/api/explore/overview?session=sess-sql")

    def _reject_expensive_graph_dependency(*_args, **_kwargs):
        raise AssertionError("lazy graph loading must not scan skill and subagent telemetry")

    monkeypatch.setattr(
        "reflect.views.report_tabs._skill_subagent_counts",
        _reject_expensive_graph_dependency,
    )
    graph = client.get("/api/explore/graph?session=sess-sql")
    context = client.get("/api/explore/context?session=sess-sql")
    missing = client.get("/api/explore/not-a-view")

    assert usage.status_code == 200
    assert usage.json()["view"] == "usage"
    assert usage.json()["scoped"] is True
    assert usage.json()["events_by_type"]["llm_call"] == 2
    assert usage.json()["events_by_type"]["tool_call"] == 1
    assert usage.json()["shell_executions"] == 1
    assert usage.json()["agent_cost_over_time"] == [
        {"day": "2026-05-01", "agent": "codex", "total_cost": 0.42},
    ]
    assert tools.json()["view"] == "tools"
    assert tools.json()["tools_by_count"] == {"exec_command": 1}
    assert graph.json()["view"] == "graph"
    assert "graph_semantic" in graph.json()
    assert context.json()["view"] == "context"
    assert {"specs", "memory", "privacy", "exports"} <= set(context.json())
    assert legacy_usage_name.json()["view"] == "usage"
    assert missing.status_code == 404
    assert "Unknown Explore view" in missing.json()["error"]


def test_dashboard_session_detail_keeps_llm_input_tokens_on_response_turns(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    now = "2026-05-01T10:01:30+00:00"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'metadata-only-llm-step', 'sess-sql', 4, 'llm_call',
              ?, 'completed', 'gen_ai.client.hook.Stop',
              '{"gen_ai.client.status":"completed","gen_ai.client.generation_id":"gen-2"}',
              ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'metadata-only-llm', 'metadata-only-llm-step', 'sess-sql',
              'openai', 'gpt-5.4', 'gpt-5.4', 44, 11, 0.01, ?, ?
            )
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    detail = TestClient(app).get("/api/session/sess-sql")

    assert detail.status_code == 200
    prompts = [event for event in detail.json()["conversation"] if event["type"] == "prompt"]
    responses = [event for event in detail.json()["conversation"] if event["type"] == "response"]
    assert len(prompts) == 1
    assert prompts[0]["preview"].startswith("Fix the failing SQL dashboard tests")
    metadata_response = next(event for event in responses if event["input_tokens"] == 44)
    assert metadata_response["output_tokens"] == 11


def test_dashboard_session_detail_keeps_one_placeholder_for_explicit_prompt_event(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    now = "2026-05-01T10:01:30+00:00"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'metadata-only-prompt-step', 'sess-sql', 4, 'conversation',
              ?, 'completed', 'gen_ai.client.hook.UserPromptSubmit',
              '{"gen_ai.client.generation_id":"gen-prompt-metadata"}',
              ?, ?
            )
            """,
            (now, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    detail = TestClient(app).get("/api/session/sess-sql")

    assert detail.status_code == 200
    prompts = [event for event in detail.json()["conversation"] if event["type"] == "prompt"]
    assert len(prompts) == 2
    assert prompts[1]["preview"] == (
        "Prompt text was not captured for this turn; metadata is available."
    )


def test_dashboard_session_filter_navigation_cards_include_first_prompts(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    _add_sql_codex_sibling_session(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data", params={"session": "sess-codex-2"})

    assert response.status_code == 200
    sessions = {session["id"]: session for session in response.json()["sessions"]}
    assert sessions["sess-sql"]["first_prompt"].startswith(
        "Fix the failing SQL dashboard tests"
    )


def test_dashboard_session_detail_shows_tokenless_stop_response_turns(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    now = "2026-05-01T10:02:30+00:00"
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            """
            INSERT INTO steps(
              id, session_id, seq, type, started_at, status, summary,
              raw_attrs_json, created_at, updated_at
            )
            VALUES (
              'tokenless-llm-step', 'sess-sql', 4, 'llm_call',
              ?, 'completed', 'gen_ai.client.hook.Stop',
              '{"gen_ai.client.status":"completed","gen_ai.client.generation_id":"gen-tokenless"}',
              ?, ?
            )
            """,
            (now, now, now),
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              input_tokens, output_tokens, estimated_cost_usd, created_at, updated_at
            )
            VALUES (
              'tokenless-llm', 'tokenless-llm-step', 'sess-sql',
              'openai', 'gpt-5.4', 'gpt-5.4', 0, 0, 0, ?, ?
            )
            """,
            (now, now),
        )
        conn.commit()
    finally:
        conn.close()
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    detail = TestClient(app).get("/api/session/sess-sql")

    assert detail.status_code == 200
    responses = [event for event in detail.json()["conversation"] if event["type"] == "response"]
    assert len(responses) == 2
    assert responses[1]["preview"] == "Assistant turn completed, but response text was not captured."
    assert responses[1]["output_tokens"] == 0


def test_dashboard_sql_sessions_endpoint_filters_from_sql(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    overview = TestClient(app).get("/api/sql/overview")
    sessions = TestClient(app).get("/api/sql/sessions", params={"agent": "codex", "model": "gpt-5.4"})

    assert overview.status_code == 200
    assert overview.json()["estimated_cost_usd"] == 0.42
    assert sessions.status_code == 200
    assert sessions.json()["total"] == 1
    assert sessions.json()["rows"][0]["agent"] == "codex"
    assert sessions.json()["rows"][0]["duration_ms"] == 120000


def test_dashboard_sql_session_detail_prefers_native_assistant_responses(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    native_session = tmp_path / "codex-session.jsonl"
    native_session.write_text(
        "\n".join([
            '{"type":"session_meta","payload":{"id":"sess-sql","model":"gpt-5.4"}}',
            '{"type":"response_item","timestamp":"2026-05-01T10:00:00Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Fix it"}]}}',
            '{"type":"response_item","timestamp":"2026-05-01T10:00:30Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"The native response is visible."}]}}',
        ]) + "\n",
        encoding="utf-8",
    )
    conn = connect_sqlite(db_path)
    try:
        conn.execute(
            "UPDATE sessions SET source_kind = 'native_session', source_ref = ? WHERE id = 'sess-sql'",
            (f"native_session:codex:{native_session}",),
        )
        conn.commit()
    finally:
        conn.close()
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/session/sess-sql")

    assert response.status_code == 200
    payload = response.json()
    assistant = next(event for event in payload["conversation"] if event["type"] == "response")
    assert assistant["content"] == "The native response is visible."
    assert payload["conversation_source"] == "native"
    assert payload["telemetry"]["summary"]["spans"] == 3


def test_dashboard_api_filters_by_session_param_from_sql(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data", params={"session": "sess-sql"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["unique_sessions"] == 1
    assert [session["id"] for session in payload["sessions"]] == ["sess-sql"]


def test_dashboard_api_applies_sql_filters_and_comparison(tmp_path, monkeypatch):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    _add_sql_baseline_session(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)
    dashboard_module = __import__("reflect.dashboard", fromlist=["_sql_dashboard_compat_payload"])
    original_compat = dashboard_module._sql_dashboard_compat_payload
    compat_calls = []

    def capture_compat(*args, **kwargs):
        compat_calls.append(kwargs)
        return original_compat(*args, **kwargs)

    monkeypatch.setattr("reflect.dashboard._sql_dashboard_compat_payload", capture_compat)

    response = TestClient(app).get(
        "/api/data",
        params={
            "agents": "codex",
            "status": "completed",
            "model": "gpt-5.4",
            "range": "7d",
            "tab": "explore",
            "view": "usage",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["unique_sessions"] == 1
    assert [session["id"] for session in payload["sessions"]] == ["sess-sql"]
    assert payload["comparison"]["primary"]["avg_quality"] == 87
    assert payload["comparison"]["baseline"]["avg_quality"] == 65
    assert payload["comparison"]["baseline_agents"][0]["avg_quality"] == 65
    assert payload["sqlite"]["tabs"]["cohort_comparison"]["comparison"] == payload["comparison"]
    assert payload["sqlite"]["tabs"]["cohort_comparison"]["agent_comparison"] == payload["agent_comparison"]
    assert {item["name"] for item in payload["sessions"][0]["quality_breakdown"]} >= {"Completion", "Efficiency"}
    assert payload["sessions"][0]["quality_breakdown"][0]["inputs"]
    assert len(compat_calls) == 1

    active = TestClient(app).get("/api/data", params={"agents": "codex", "status": "active"})
    assert active.status_code == 200
    assert active.json()["unique_sessions"] == 0


def test_dashboard_api_session_scope_wins_with_agent_filter(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    _add_sql_baseline_session(db_path)
    _add_sql_codex_sibling_session(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data", params={"agents": "codex", "session": "sess-sql"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["unique_sessions"] == 1
    assert payload["session_list_total"] == 2
    assert [session["id"] for session in payload["sessions"]] == ["sess-codex-2", "sess-sql"]
    assert payload["activity_by_day"] == {"2026-05-01": 3}
    assert payload["sqlite"]["tabs"]["activity"]["activity_by_day"] == {"2026-05-01": 3}
    assert payload["comparison"] is None


def test_dashboard_api_scopes_sql_tab_view_models(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get("/api/data", params={"agents": "missing-agent"})

    assert response.status_code == 200
    payload = response.json()
    tabs = payload["sqlite"]["tabs"]
    assert payload["unique_sessions"] == 0
    assert payload["activity_by_day"] == {}
    assert tabs["activity"]["activity_by_day"] == {}
    assert tabs["activity"]["events_by_type"] == {}
    assert payload["graph_dep"]["nodes"] == []
    assert tabs["graphs"]["graph_dep"]["nodes"] == []
    assert tabs["graphs"]["graph_tool_transitions"] == []
    assert tabs["tools"]["tools_by_count"] == {}
    assert tabs["mcp"]["mcp_servers_by_count"] == {}


def test_dashboard_usage_empty_scope_keeps_widget_contracts_empty(tmp_path):
    db_path = tmp_path / "reflect.db"
    _seed_sql_report_db(db_path)
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=db_path)

    response = TestClient(app).get(
        "/api/data",
        params={
            "agents": "missing-agent",
            "tab": "explore",
            "view": "usage",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    usage = payload["sqlite"]["tabs"]["usage"]
    assert payload["unique_sessions"] == 0
    assert usage["unique_sessions"] == 0
    assert usage["models_by_count"] == {}
    assert usage["events_by_type"] == {}
    assert usage["source_provenance"] == []
    assert usage["agent_cost_over_time"] == []
    assert usage["subagent_types_by_count"] == {}
    assert payload["weekly_trends"] == []
    assert payload["failure_rate_pct"] == 0


def test_dashboard_sql_endpoints_are_disabled_without_db(tmp_path):
    app = _build_dashboard_app(_stats(), docs_dir=tmp_path, db_path=None)

    response = TestClient(app).get("/api/sql/overview")

    assert response.status_code == 404
    assert "not configured" in response.json()["error"]

    filtered = TestClient(app).get("/api/data", params={"agents": "codex"})
    assert filtered.status_code == 409
    assert filtered.json()["sql_backed"] is False
    assert "SQLite report store" in filtered.json()["error"]
