"""Derived projections must consume canonical changes before acknowledging them."""
import json
from contextlib import closing

import pytest

from reflect.preparation_pipeline import prepare_sql_report_db, prepare_usage_db
from reflect.store.git_activity import git_activity
from reflect.store.graph_normalize import rebuild_graph
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.sqlite import connect_sqlite, snapshot_incomplete


def _event(name, span, attrs):
    at = 1780000000000000000 + span * 1_000_000_000
    return {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": name, "traceId": "trace", "spanId": str(span),
        "startTimeUnixNano": str(at), "endTimeUnixNano": str(at + 100),
        "attributes": [{"key": key, "value": {"stringValue": value}} for key, value in {
            "gen_ai.client.name": "claude", "gen_ai.client.session_id": "session",
            **attrs,
        }.items()],
    }]}]}]}


@pytest.fixture
def projection_store(tmp_path, monkeypatch):
    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("reflect.pricing._fetch_json_url", lambda *args, **kwargs: {})
    db, traces = tmp_path / "reflect.db", tmp_path / "otel-traces.active.jsonl"
    traces.write_text(json.dumps(_event("UserPromptSubmit", 1, {})) + "\n")
    prepare_sql_report_db(db, otlp_traces=traces)
    return db, traces


def _append(traces, name, attrs, *, span=2):
    with traces.open("a") as stream:
        stream.write(json.dumps(_event(name, span, attrs)) + "\n")


def _append_git(traces):
    _append(traces, "PostToolUse", {
        "gen_ai.client.tool_name": "exec_command",
        "gen_ai.client.tool_use_id": "git-create",
        "gen_ai.client.tool.input": json.dumps({"cmd": "glab mr create --title example"}),
        "gen_ai.client.tool.output": json.dumps({"exit_code": 0,
            "output": "https://git.example.test/group/project/-/merge_requests/42"}),
    })


def test_usage_then_report_preserves_confirmed_git_activity(projection_store):
    db, traces = projection_store
    _append_git(traces)
    prepare_usage_db(db, otlp_traces=traces, include_native_sessions=False)
    with closing(connect_sqlite(db)) as conn:
        assert git_activity(conn)["created_merge_requests"] == 1
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert git_activity(conn)["created_merge_requests"] == 1
        assert not PendingRefreshState(conn).capture().pending_for("canonical")


@pytest.mark.parametrize("profile", ["usage", "report"])
def test_context_repair_updates_git_repository_without_new_calls(projection_store, profile):
    db, traces = projection_store
    _append_git(traces)
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        now = "2026-05-28T00:00:00Z"
        conn.execute("INSERT INTO repos(id,name,full_name,created_at,updated_at) VALUES('old','old','old/project',?,?)", (now, now))
        conn.execute("INSERT INTO workspaces(id,root_path,path_hash,label,source_key,created_at,updated_at) VALUES('generic','/','generic','generic','fixture',?,?)", (now, now))
        conn.execute("UPDATE sessions SET workspace_id='generic',repo_id='old' WHERE id='session'")
        conn.execute("UPDATE evidence SET repo_id='old' WHERE kind='vcs_operation'")
        conn.commit()
        assert not PendingRefreshState(conn).capture().pending_for()
    if profile == "usage":
        prepare_usage_db(db, otlp_traces=traces, include_native_sessions=False)
    else:
        prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT repo_id FROM sessions WHERE id='session'").fetchone()[0] is None
        assert conn.execute("SELECT repo_id FROM evidence WHERE kind='vcs_operation'").fetchone()[0] is None
        assert not PendingRefreshState(conn).capture().pending_for("canonical")


@pytest.mark.parametrize("profile", ["usage", "report"])
@pytest.mark.parametrize("after_projection", [False, True])
def test_interrupted_git_projection_keeps_canonical_scope_for_restart(
    projection_store, monkeypatch, profile, after_projection,
):
    from reflect.store.git_activity import refresh_git_activity

    db, traces = projection_store
    _append_git(traces)

    def interrupt(conn, *, session_ids=None):
        if after_projection:
            refresh_git_activity(conn, session_ids=session_ids)
        raise RuntimeError("Git projection interrupted")

    with monkeypatch.context() as patch:
        patch.setattr("reflect.store.git_activity.refresh_git_activity", interrupt)
        with pytest.raises(RuntimeError, match="Git projection interrupted"):
            if profile == "usage":
                prepare_usage_db(db, otlp_traces=traces, include_native_sessions=False)
            else:
                prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert PendingRefreshState(conn).capture().pending_for("canonical") == {"session"}
        assert snapshot_incomplete(conn)
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert git_activity(conn)["created_merge_requests"] == 1
        assert not PendingRefreshState(conn).capture().pending_for()
        assert not snapshot_incomplete(conn)


def _graph_content(conn):
    return [tuple(row) for row in conn.execute(
        "SELECT id,kind,label,session_id,first_seen_at,last_seen_at,attrs_json FROM graph_nodes ORDER BY id"
    )], [tuple(row) for row in conn.execute(
        "SELECT id,source_node_id,target_node_id,kind,session_id,weight,first_seen_at,last_seen_at,attrs_json "
        "FROM graph_edges ORDER BY id"
    )]


@pytest.mark.parametrize("memory_type,node_kind", [("repo_convention", "Memory"), ("cursor_plan", "Spec")])
def test_normalized_memory_append_matches_full_graph_repair(projection_store, memory_type, node_kind):
    db, traces = projection_store
    _append(traces, "MemoryWrite", {
        "gen_ai.memory.id": "memory", "gen_ai.memory.type": memory_type,
        "gen_ai.memory.content_hash": "hash", "gen_ai.memory.content_preview": "Use pytest",
        "gen_ai.memory.scope": "repo",
    })
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind=?", (node_kind,)).fetchone()[0] == 1
        assert not PendingRefreshState(conn).capture().pending_for("graph")
        incremental = _graph_content(conn)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental


def test_existing_memory_exposure_append_matches_full_graph_repair(projection_store):
    db, traces = projection_store
    attrs = {"gen_ai.memory.id": "shared-memory", "gen_ai.memory.type": "instruction",
             "gen_ai.memory.content_hash": "hash", "gen_ai.memory.scope": "repo"}
    _append(traces, "MemoryWrite", attrs)
    prepare_sql_report_db(db, otlp_traces=traces)
    _append(traces, "MemoryRead", {**attrs, "gen_ai.client.session_id": "other"}, span=3)
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_exposures").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM graph_edges WHERE kind='recorded_memory'").fetchone()[0] == 2
        incremental = _graph_content(conn)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental


def test_upgrade_heals_previously_acknowledged_git_and_memory_projections(projection_store):
    db, traces = projection_store
    _append_git(traces)
    _append(traces, "MemoryWrite", {"gen_ai.memory.id": "memory", "gen_ai.memory.type": "instruction"}, span=3)
    prepare_sql_report_db(db, otlp_traces=traces)
    with closing(connect_sqlite(db)) as conn:
        assert not PendingRefreshState(conn).capture().pending_for()
        # Model the previous version's acknowledged projections without new
        # capture. Removing only the migration checkpoint exercises upgrade.
        conn.execute("DELETE FROM evidence WHERE kind='vcs_operation'")
        conn.execute("DELETE FROM graph_nodes WHERE kind='Memory'")
        conn.execute("DELETE FROM schema_migrations WHERE version=49")
        assert git_activity(conn)["created_merge_requests"] == 0
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind='Memory'").fetchone()[0] == 0
        conn.commit()
    prepare_sql_report_db(db, otlp_traces=traces, analysis_session_ids=set())
    with closing(connect_sqlite(db)) as conn:
        assert git_activity(conn)["created_merge_requests"] == 1
        assert conn.execute("SELECT COUNT(*) FROM graph_nodes WHERE kind='Memory'").fetchone()[0] == 1
        assert not PendingRefreshState(conn).capture().pending_for("canonical")
        assert not PendingRefreshState(conn).capture().pending_for("graph")
        incremental = _graph_content(conn)
        rebuild_graph(conn)
        assert _graph_content(conn) == incremental
