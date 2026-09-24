import sqlite3

from reflect.guidance_efficiency import GuidanceEfficiencyService
from reflect.improvements.rules import ContextExplosionRule


def test_guidance_comparison_keeps_model_and_usage_evidence_separate():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE execution_units (
            id TEXT, repo_id TEXT, mcp_task_run_id TEXT, outcome TEXT,
            verification_passed INTEGER, eligible INTEGER, status TEXT, started_at TEXT);
        CREATE TABLE execution_unit_archetypes (execution_unit_id TEXT, task_archetype_id TEXT);
        CREATE TABLE mcp_task_runs (id TEXT, status TEXT, skill_usage_recorded_count INTEGER);
        CREATE TABLE execution_unit_steps (execution_unit_id TEXT, step_id TEXT);
        CREATE TABLE llm_calls (
            id TEXT, step_id TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, estimated_cost_usd REAL);
        CREATE TABLE tool_calls (step_id TEXT);
        INSERT INTO mcp_task_runs VALUES ('run', 'completed', 1);
        INSERT INTO execution_units VALUES
            ('guided', 'repo', 'run', 'success', 1, 1, 'completed', '2026-01-02'),
            ('baseline', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-01'),
            ('other-model', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-03');
        INSERT INTO execution_unit_archetypes VALUES
            ('guided', 'review'), ('baseline', 'review'), ('other-model', 'review');
        INSERT INTO execution_unit_steps VALUES
            ('guided', 'g'), ('baseline', 'b'), ('other-model', 'o');
        INSERT INTO llm_calls VALUES
            ('g1', 'g', 'model-a', 'model-a', 20, 10, 200, 30, 0.5),
            ('b1', 'b', 'model-a', 'model-a', 50, 10, 0, 0, 0.3),
            ('o1', 'o', 'model-b', 'model-b', 50, 10, 0, 0, 0.3);
        INSERT INTO tool_calls VALUES ('g'), ('g'), ('b');
    """)
    result = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a"
    )
    assert result["guided"]["count"] == result["unguided"]["count"] == 1
    assert result["guided"]["recorded_skill_use_count"] == 1
    assert result["guided"]["mean_billed_tokens"] == 260
    assert result["unguided"]["mean_billed_tokens"] == 60
    assert result["guided"]["mean_tool_calls"] == 2
    assert result["excluded"]["mixed_or_other_model"] == 1
    assert result["guided"]["mean_estimated_cost_usd"] == 0.5
    conn.execute("UPDATE llm_calls SET estimated_cost_usd = 0 WHERE id = 'g1'")
    unpriced = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a"
    )
    assert unpriced["guided"]["mean_estimated_cost_usd"] is None


def test_context_explosion_counts_cache_reads():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE sessions (
        id TEXT, repo_id TEXT, input_tokens INTEGER, output_tokens INTEGER,
        cache_read_tokens INTEGER, cache_creation_tokens INTEGER)""")
    conn.executemany(
        "INSERT INTO sessions VALUES (?, 'repo', ?, 0, ?, 0)",
        [("a", 10_000, 0), ("b", 10_000, 0), ("c", 10_000, 300_000)],
    )
    findings = ContextExplosionRule().detect(conn)
    assert len(findings) == 1
    assert findings[0].metric_value == 1
    assert "310,000" in findings[0].source_sessions[0].summary_redacted
