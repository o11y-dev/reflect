import json

from reflect.store.graph_normalize import rebuild_graph
from reflect.store.ingest import ingest_local_spans_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def _write_spans(path):
    spans = [
        {
            "name": "UserPromptSubmit",
            "traceId": "trace-1",
            "spanId": "span-1",
            "start_time_ns": 100,
            "end_time_ns": 200,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.request.model": "claude-4.6-opus",
            },
        },
        {
            "name": "PreToolUse",
            "traceId": "trace-1",
            "spanId": "span-2",
            "start_time_ns": 300,
            "end_time_ns": 500,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.client.tool_name": "Read",
            },
        },
        {
            "name": "BeforeMCPExecution",
            "traceId": "trace-1",
            "spanId": "span-3",
            "start_time_ns": 600,
            "end_time_ns": 800,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.client.mcp_server": "mcp-github",
                "gen_ai.client.mcp_tool": "get_issue",
            },
        },
        {
            "name": "MemoryWrite",
            "traceId": "trace-1",
            "spanId": "span-4",
            "start_time_ns": 900,
            "end_time_ns": 1000,
            "attributes": {
                "gen_ai.client.name": "claude",
                "gen_ai.client.session_id": "sess-graph",
                "gen_ai.memory.id": "mem-graph",
                "gen_ai.memory.scope": "repo",
                "gen_ai.memory.type": "repo_convention",
            },
        },
    ]
    path.write_text("\n".join(json.dumps(span) for span in spans) + "\n", encoding="utf-8")


def test_rebuild_graph_from_canonical_tables_is_idempotent(tmp_path):
    db = tmp_path / "reflect.db"
    spans = tmp_path / "spans.jsonl"
    _write_spans(spans)

    conn = connect_sqlite(db)
    try:
        migrate(conn)
        ingest_local_spans_file(conn, file_path=spans)
        normalize_pending_raw_events(conn)

        first = rebuild_graph(conn)
        second = rebuild_graph(conn)

        assert first["nodes"] >= 8
        assert first["edges"] >= 7
        assert second == {"nodes": 0, "edges": 0}
        node_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_nodes")}
        assert {"Session", "Step", "Agent", "Tool", "MCPServer", "Memory"} <= node_kinds
        edge_kinds = {row[0] for row in conn.execute("SELECT DISTINCT kind FROM graph_edges")}
        assert {"ran_session", "has_step", "used_tool", "used_mcp", "recorded_memory"} <= edge_kinds
    finally:
        conn.close()
