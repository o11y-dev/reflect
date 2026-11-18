import sqlite3

import pytest

from reflect.guidance_efficiency import GuidanceEfficiencyService
from reflect.improvements.rules import ContextExplosionRule


def _guidance_connection():
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
    conn.executescript("""
        ALTER TABLE execution_units ADD COLUMN boundary_json TEXT DEFAULT '{}';
        ALTER TABLE execution_units ADD COLUMN session_id TEXT;
        UPDATE execution_units SET session_id=id;
        CREATE TABLE pending_refresh(phase TEXT, session_id TEXT, revision TEXT, deferred INTEGER DEFAULT 0);
        ALTER TABLE llm_calls ADD COLUMN raw_attrs_json TEXT DEFAULT '{"reflect.cost.source":"fallback"}';
        UPDATE llm_calls SET raw_attrs_json = json_object('reflect.cost.source','fallback',
            'gen_ai.usage.input_tokens', input_tokens, 'gen_ai.usage.output_tokens', output_tokens,
            'gen_ai.usage.cache_read.input_tokens', cache_read_input_tokens,
            'gen_ai.usage.cache_creation.input_tokens', cache_creation_input_tokens);
        UPDATE execution_units SET boundary_json = '{"assessment":{"version":1,"exposure":"observed"}}' WHERE id='guided';
        UPDATE execution_units SET boundary_json = '{"assessment":{"version":1,"exposure":"observed_absent"}}' WHERE id='baseline';
    """)
    return conn


def test_guidance_comparison_keeps_model_and_usage_evidence_separate():
    conn = _guidance_connection()
    result = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a", limit=2
    )
    assert result["guided"]["count"] == result["unguided"]["count"] == 1
    assert result["guided"]["reported_selected_skill_count"] == 1
    assert result["guided"]["mean_observed_tokens"] == 260
    assert result["guided"]["token_mix"] == {
        "uncached_input": 20,
        "cache_read_input": 200,
        "cache_creation_input": 30,
        "output": 10,
        "reasoning_output": 5,
        "observed_cache_read_share_of_input": 0.8,
    }
    assert result["guided"]["mean_input_tokens_per_model_call"] == 250
    assert result["guided"]["verified_success_rate"] == 1
    assert result["guided"]["estimated_cost_per_verified_success_usd"] == 0.5
    assert result["unguided"]["mean_observed_tokens"] == 60
    assert result["guided"]["mean_tool_calls"] == 2
    assert result["excluded"]["mixed_or_other_model"] == 2
    assert result["scanned"] == 4
    assert result["guided"]["mean_estimated_cost_usd"] == 0.5
    conn.executescript("""
        INSERT INTO execution_units VALUES
            ('failed', 'repo', 'run', 'failure', 0, 1, 'completed', '2026-01-06', '{"assessment":{"version":1,"exposure":"observed"}}', 'failed');
        INSERT INTO execution_unit_archetypes VALUES ('failed', 'review', 0, 0.9);
        INSERT INTO execution_unit_steps VALUES ('failed', 'f');
        INSERT INTO llm_calls VALUES
            ('f1', 'f', 'model-a', 'model-a', 20, 10, 0, 0, 0.2, 0, '{"reflect.cost.source":"fallback","gen_ai.usage.input_tokens":20,"gen_ai.usage.output_tokens":10,"gen_ai.usage.cache_read.input_tokens":0,"gen_ai.usage.cache_creation.input_tokens":0}');
    """)
    with_failure = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a", limit=10
    )
    assert with_failure["guided"]["verified_success_rate"] == 0.5
    assert with_failure["guided"]["mean_estimated_cost_usd"] == 0.35
    assert with_failure["guided"]["estimated_cost_per_verified_success_usd"] == 0.7
    # Reports/exposure never establish an assessed outcome. Keep its usage,
    # while excluding it from the success denominator and success cost.
    conn.execute("UPDATE execution_units SET outcome=NULL, verification_passed=NULL, boundary_json='{}' WHERE id='baseline'")
    unknown = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a"
    )["unknown_exposure"]
    assert unknown["count"] == unknown["unknown_outcome_count"] == 1
    assert unknown["observed_estimated_cost_usd"] == 0.3
    assert unknown["mean_observed_tokens"] == 60
    assert unknown["verified_success_rate"] is None
    assert unknown["estimated_cost_per_verified_success_usd"] is None

    # A model rate does not mean all usage buckets were captured.
    conn.execute("UPDATE llm_calls SET raw_attrs_json=json_remove(raw_attrs_json, '$.\"gen_ai.usage.cache_read.input_tokens\"') WHERE id='g1'")
    partial = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a"
    )["guided"]
    assert partial["cost_coverage"]["priced_calls"] == 2
    assert partial["cost_coverage"]["complete_usage_calls"] == 1
    assert partial["estimated_cost_per_verified_success_usd"] is None
    assert partial["observed_estimated_cost_usd"] == 0.7
    conn.execute("UPDATE llm_calls SET estimated_cost_usd = 0, raw_attrs_json = '{}' WHERE id = 'g1'")
    unpriced = GuidanceEfficiencyService(conn).compare(
        repo_id="repo", task_archetype_id="review", model="model-a"
    )
    assert unpriced["guided"]["mean_estimated_cost_usd"] is None
    assert unpriced["guided"]["estimated_cost_per_verified_success_usd"] is None
    assert unpriced["guided"]["unpriced_model_calls"] == 1


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
    assert "300,000" in findings[0].source_sessions[0].summary_redacted


@pytest.mark.parametrize(("phase", "deferred"), [("canonical", 0), ("cost", 0), ("improvements", 0), ("improvements", 1)])
def test_pending_execution_keeps_usage_but_cannot_supply_success_or_exposure(phase, deferred):
    conn = _guidance_connection()
    conn.execute("INSERT INTO pending_refresh VALUES (?, 'guided', 'revision', ?)", (phase, deferred))
    result = GuidanceEfficiencyService(conn).compare(repo_id="repo", task_archetype_id="review", model="model-a")
    pending = result["unknown_exposure"]
    assert result["guided"]["count"] == 0
    assert result["unguided"]["verified_success_count"] == 1
    assert pending["count"] == pending["unknown_outcome_count"] == 1
    assert pending["evidence_coverage"] == {"current_execution_units": 0, "pending_execution_units": 1}
    assert pending["observed_estimated_cost_usd"] == 0.5
    assert pending["mean_observed_tokens"] == 260
    assert pending["verified_success_count"] == 0
    assert pending["verified_success_rate"] is None
    assert pending["estimated_cost_per_verified_success_usd"] is None
    assert pending["mean_estimated_cost_usd"] is None
    conn.close()


def test_graph_only_pending_does_not_invalidate_independent_guidance_assessment():
    conn = _guidance_connection()
    conn.execute("INSERT INTO pending_refresh VALUES ('graph', 'guided', 'revision', 1)")
    result = GuidanceEfficiencyService(conn).compare(repo_id="repo", task_archetype_id="review", model="model-a")
    assert result["guided"]["verified_success_count"] == 1
    assert result["guided"]["evidence_coverage"]["pending_execution_units"] == 0
    conn.close()
