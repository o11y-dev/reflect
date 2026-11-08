from reflect.dashboard_query_common import sql_insight_payload


def test_legacy_insights_use_scoped_facts_without_inventing_effectiveness():
    result = sql_insight_payload(
        {"session_count": 900, "input_tokens": 100, "output_tokens": 10,
         "tool_call_count": 5, "failure_count": 0, "estimated_cost_usd": 2},
        [{"prompt_count": 100, "total_tokens": 999_999}],
        {"total_cache_creation_tokens": 0, "total_cache_read_tokens": 50,
         "mcp_calls": 2, "subagent_total_starts": 0, "unique_models": 1, "unique_commands": 1},
    )
    assert result["achievements"][0]["sub"] == "900 captured sessions"
    names = {item["name"] for item in result["achievements"]}
    assert "Cache Tokens" in names
    assert not names & {"Cache Saver", "Zero Failures"}
    assert result["token_economy"]["top_session_share"] is None
    assert result["token_economy"]["cache_reuse_ratio"] is None
    assert result["recommendations"] == []  # The R5 rule registry owns advice.
