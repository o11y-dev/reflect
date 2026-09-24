import sqlite3

from reflect.guidance_efficiency import GuidanceEfficiencyService
from reflect.improvements.rules import ContextExplosionRule


def test_guidance_comparison_keeps_model_and_usage_evidence_separate():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE execution_units (
            id TEXT, repo_id TEXT, mcp_task_run_id TEXT, outcome TEXT,
            verification_passed INTEGER, eligible INTEGER, status TEXT, started_at TEXT);
        CREATE TABLE execution_unit_archetypes (
            execution_unit_id TEXT, task_archetype_id TEXT, mixed INTEGER, confidence REAL);
        CREATE TABLE mcp_task_runs (id TEXT, status TEXT, skill_usage_recorded_count INTEGER);
        CREATE TABLE execution_unit_steps (execution_unit_id TEXT, step_id TEXT);
        CREATE TABLE llm_calls (
            id TEXT, step_id TEXT, request_model TEXT, response_model TEXT,
            input_tokens INTEGER, output_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_input_tokens INTEGER, estimated_cost_usd REAL,
            reasoning_output_tokens INTEGER);
        CREATE TABLE tool_calls (step_id TEXT);
        INSERT INTO mcp_task_runs VALUES ('run', 'completed', 1);
        INSERT INTO execution_units VALUES
            ('guided', 'repo', 'run', 'success', 1, 1, 'completed', '2026-01-02'),
            ('baseline', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-01'),
            ('other-model', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-03'),
            ('unknown-model', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-04'),
            ('mixed-archetype', 'repo', NULL, 'success', 1, 1, 'completed', '2026-01-05');
        INSERT INTO execution_unit_archetypes VALUES
            ('guided', 'review', 0, 0.9), ('baseline', 'review', 0, 0.9),
            ('other-model', 'review', 0, 0.9), ('unknown-model', 'review', 0, 0.9),
            ('mixed-archetype', 'review', 1, 0.9);
        INSERT INTO execution_unit_steps VALUES
            ('guided', 'g'), ('baseline', 'b'), ('other-model', 'o'),
            ('unknown-model', 'u'), ('mixed-archetype', 'm');
        INSERT INTO llm_calls VALUES
            ('g1', 'g', 'model-a', 'model-a', 20, 10, 200, 30, 0.5, 5),
            ('b1', 'b', 'model-a', 'model-a', 50, 10, 0, 0, 0.3, 0),
            ('o1', 'o', 'model-b', 'model-b', 50, 10, 0, 0, 0.3, 0),
            ('u1', 'u', 'model-a', 'model-a', 50, 10, 0, 0, 0.3, 0),
            ('u2', 'u', NULL, NULL, 50, 10, 0, 0, 0.3, 0),
            ('m1', 'm', 'model-a', 'model-a', 50, 10, 0, 0, 0.3, 0);
        INSERT INTO tool_calls VALUES ('g'), ('g'), ('b');
    """)
    result = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a", limit=2
    )
    assert result["guided"]["count"] == result["unguided"]["count"] == 1
    assert result["guided"]["reported_selected_skill_count"] == 1
    assert result["guided"]["mean_billed_tokens"] == 265
    assert result["unguided"]["mean_billed_tokens"] == 60
    assert result["guided"]["mean_tool_calls"] == 2
    assert result["excluded"]["mixed_or_other_model"] == 2
    assert result["scanned"] == 4
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
        cache_read_tokens INTEGER, cache_creation_tokens INTEGER,
        reasoning_tokens INTEGER)""")
    conn.executemany(
        "INSERT INTO sessions VALUES (?, 'repo', ?, 0, ?, 0, ?)",
        [("a", 10_000, 0, 0), ("b", 10_000, 0, 0), ("c", 10_000, 290_000, 10_000)],
    )
    findings = ContextExplosionRule().detect(conn)
    assert len(findings) == 1
    assert findings[0].metric_value == 1
    assert "310,000" in findings[0].source_sessions[0].summary_redacted
