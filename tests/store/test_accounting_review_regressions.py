"""Redacted captured envelope shapes for the 2026-11 accounting review."""
import copy
import json
from pathlib import Path

import pytest

from reflect.parsing import _native_tool_result_metadata
from reflect.store.ingest import ingest_local_spans_file, ingest_native_session_file
from reflect.store.migrate import migrate
from reflect.store.normalize import normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite
from reflect.views.measurements import build_measurements

FIXTURES = json.loads((Path(__file__).parents[1] / "fixtures/capture/accounting_review.json").read_text())


@pytest.fixture
def conn(tmp_path):
    db = connect_sqlite(tmp_path / "state.db")
    migrate(db)
    yield db
    db.close()


def write_records(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


def normalize(conn):
    assert normalize_pending_raw_events(conn)["failed"] == 0


def native(conn, path, records):
    write_records(path, records)
    result = ingest_native_session_file(conn, file_path=path, agent="codex", skip_unchanged=True)
    normalize(conn)
    return result


def test_subagent_usage_survives_reconciliation_and_raw_replay(conn, tmp_path):
    path = tmp_path / "hooks.jsonl"
    write_records(path, [FIXTURES["agent_stop"]])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    for _ in range(2):
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
        assert conn.execute("SELECT type FROM steps").fetchone()[0] == "agent_call"
        view = build_measurements(conn)
        assert (view.totals["input_tokens"], view.totals["output_tokens"]) == (100, 50)
        assert view.coverage["observed_usage"] == 1
        conn.execute("UPDATE raw_events SET normalized_status='pending'")
        conn.commit()
        normalize(conn)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


@pytest.mark.parametrize("agent_first", [True, False])
def test_agent_and_request_observations_share_explicit_request_identity(conn, tmp_path, agent_first):
    agent = copy.deepcopy(FIXTURES["agent_stop"])
    agent["attributes"]["gen_ai.request.id"] = "redacted-request"
    request = copy.deepcopy(agent)
    request["name"] = request["attributes"]["gen_ai.client.hook.event"] = "Usage"
    request["spanId"] = "redacted-parent-observation"
    request["attributes"].pop("gen_ai.client.agent_id")
    path = tmp_path / "hooks.jsonl"
    write_records(path, [agent, request] if agent_first else [request, agent])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 2
    for _ in range(2):
        conn.execute("UPDATE raw_events SET normalized_status='pending'")
        conn.commit()
        normalize(conn)
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
        assert build_measurements(conn).coverage["observed_usage"] == 1


def test_native_usage_enrichment_cannot_duplicate_source_owned_agent_usage(conn, tmp_path):
    path = tmp_path / "hooks.jsonl"
    write_records(path, [FIXTURES["agent_stop"]])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    claude = tmp_path / "claude.jsonl"
    write_records(claude, [{"sessionId": "redacted-session", "type": "assistant", "timestamp": "2026-11-09T10:01:00Z", "message": {"id": "redacted-message", "usage": {"input_tokens": 100, "output_tokens": 50}, "content": []}}])
    ingest_native_session_file(conn, file_path=claude, agent="claude")
    normalize(conn)
    assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
    assert build_measurements(conn).coverage["observed_usage"] == 1


def test_retained_agent_usage_is_requeued_by_migration(conn, tmp_path):
    path = tmp_path / "hooks.jsonl"
    write_records(path, [FIXTURES["agent_stop"]])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    # Simulate the reviewed projection: captured agent step retained, usage lost.
    conn.execute("DELETE FROM llm_calls")
    conn.execute("UPDATE sessions SET input_tokens=0,output_tokens=0")
    conn.execute("DELETE FROM schema_migrations WHERE version=49")
    conn.commit()
    assert 49 in migrate(conn)
    assert conn.execute("SELECT normalized_status FROM raw_events").fetchone()[0] == "pending"
    normalize(conn)
    assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
    assert migrate(conn) == []


def test_unknown_interval_usage_remains_lifetime_only_and_delta_is_dated(conn, tmp_path):
    records = [
        {"type": "session_meta", "timestamp": "2026-10-01T10:00:00Z", "payload": {"id": "redacted-cumulative"}},
        {"type": "event_msg", "timestamp": "2026-11-09T10:00:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10000, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 100}}}},
        {"type": "event_msg", "timestamp": "2026-11-09T10:01:00Z", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10100, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 110}}}},
    ]
    path = tmp_path / "native.jsonl"
    native(conn, path, records)
    conn.execute("UPDATE llm_calls SET estimated_cost_usd=1,raw_attrs_json=json_set(raw_attrs_json, '$.\"reflect.cost.source\"', 'fixture')")
    conn.commit()
    for _ in range(2):
        lifetime = build_measurements(conn)
        dated = build_measurements(conn, date_from="2026-11-09T00:00:00Z", date_to="2026-11-10T00:00:00Z")
        assert (lifetime.totals["input_tokens"], lifetime.totals["output_tokens"]) == (10100, 110)
        assert (dated.totals["input_tokens"], dated.totals["output_tokens"]) == (100, 10)
        assert lifetime.coverage["undated_usage"] == dated.coverage["undated_usage"] == 1
        assert lifetime.totals["estimated_cost_usd"] == 2
        assert dated.totals["estimated_cost_usd"] == 1
        assert lifetime.coverage["undated_cost_usd"] == 1
        assert lifetime.cost_by_day == {"2026-11-09": 1}
        assert lifetime.coverage["usage_grains"] == {"session": 1, "interval": 1}
        assert dated.coverage["usage_grains"] == {"interval": 1}
        native(conn, path, records)


def tool_records(output):
    sequence = [
        ("session_meta", {"id": "redacted-tool-session"}),
        ("turn_context", {"turn_id": "redacted-turn", "model": "recorded-model"}),
        ("response_item", {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Run the check"}]}),
        ("response_item", {"type": "function_call", "name": "exec_command", "call_id": "redacted-call", "arguments": '{"cmd":"pytest -q"}'}),
        ("response_item", {"type": "function_call_output", "call_id": "redacted-call", "output": output}),
        ("response_item", {"type": "message", "role": "assistant", "channel": "final", "content": [{"type": "output_text", "text": "Started"}]}),
    ]
    return [{"type": kind, "payload": payload, "timestamp": f"2026-11-09T10:00:{index:02d}Z"} for index, (kind, payload) in enumerate(sequence)]


@pytest.mark.parametrize("output", FIXTURES["running_envelopes"])
def test_running_envelope_is_nonterminal_until_later_result(conn, tmp_path, output):
    assert _native_tool_result_metadata(output) == {"reflect.tool.terminal": False}
    records = tool_records(output)
    path = tmp_path / "native.jsonl"
    native(conn, path, records)
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] is None
    records.insert(-1, {"type": "response_item", "timestamp": "2026-11-09T10:00:04.500Z", "payload": {"type": "function_call_output", "call_id": "redacted-call", "output": FIXTURES["terminal_envelopes"][0]}})
    native(conn, path, records)
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] == 1
    assert conn.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0] == 1
    native(conn, path, records)
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] == 1


@pytest.mark.parametrize("output", FIXTURES["terminal_envelopes"])
def test_terminal_envelope_ignores_running_text_in_stdout(conn, tmp_path, output):
    assert _native_tool_result_metadata(output) == {"process.exit.code": 0, "reflect.tool.terminal": True}
    native(conn, tmp_path / "native.jsonl", tool_records(output))
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] == 1


def test_decoder_upgrade_repairs_retained_running_tool_projection(conn, tmp_path):
    path = tmp_path / "native.jsonl"
    records = tool_records(FIXTURES["running_envelopes"][0])
    native(conn, path, records)
    conn.execute("UPDATE tool_calls SET raw_attrs_json=json_set(raw_attrs_json, '$.\"reflect.tool.terminal\"', json('true'))")
    conn.execute("UPDATE source_ingestion_state SET decoder_version=3")
    conn.commit()
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] == 1
    result = native(conn, path, records)
    assert result["replaced_native_events"] > 0
    assert build_measurements(conn).metrics["max_tool_calls_per_turn"] is None


def test_agent_repair_does_not_add_usage_over_retained_request_observation(conn, tmp_path):
    agent = copy.deepcopy(FIXTURES["agent_stop"])
    agent["attributes"]["gen_ai.request.id"] = "redacted-request"
    request = copy.deepcopy(agent)
    request["spanId"] = "redacted-retained-request"
    request["name"] = request["attributes"]["gen_ai.client.hook.event"] = "Usage"
    request["attributes"].pop("gen_ai.client.agent_id")
    path = tmp_path / "hooks.jsonl"
    write_records(path, [request])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    # The reviewed hook normalizer keyed llm_calls by raw event, not request.
    conn.execute("UPDATE llm_calls SET id='retained-legacy-request'")
    conn.commit()
    write_records(path, [request, agent])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
    assert build_measurements(conn).coverage["observed_usage"] == 1


def test_incomplete_text_block_is_not_a_running_envelope():
    assert _native_tool_result_metadata([{"type": "text", "text": "Script running with cell ID "}]) == {"reflect.tool.terminal": True}


@pytest.mark.parametrize("first_observation", ["model_only", "explicit_zero"])
def test_reconciliation_uses_same_observed_usage_identity_as_reader(conn, tmp_path, first_observation):
    agent = copy.deepcopy(FIXTURES["agent_stop"])
    agent["attributes"]["gen_ai.request.id"] = "redacted-request"
    request = copy.deepcopy(agent)
    request["spanId"] = "redacted-earlier-request"
    request["name"] = request["attributes"]["gen_ai.client.hook.event"] = "Stop"
    request["attributes"].pop("gen_ai.client.agent_id")
    request["start_time_ns"] -= 1_000_000_000
    request["end_time_ns"] -= 1_000_000_000
    for key in ("gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens"):
        if first_observation == "model_only":
            request["attributes"].pop(key)
        else:
            request["attributes"][key] = 0
    path = tmp_path / "hooks.jsonl"
    write_records(path, [request, agent])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    expected = 100 if first_observation == "model_only" else 0
    assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == expected
    assert build_measurements(conn).totals["input_tokens"] == expected


def test_usage_reconciliation_dirties_dependents_only_when_buckets_change(conn, tmp_path):
    from reflect.store.normalize import refresh_session_usage
    from reflect.store.pending_refresh import PendingRefreshState

    path = tmp_path / "hooks.jsonl"
    write_records(path, [FIXTURES["agent_stop"]])
    ingest_local_spans_file(conn, file_path=path)
    normalize(conn)
    conn.execute("DELETE FROM pending_refresh")
    conn.execute("UPDATE sessions SET input_tokens=200,output_tokens=100")
    conn.commit()
    refresh_session_usage(conn, {"redacted-session"})
    state = PendingRefreshState(conn)
    assert state.capture().pending_for("cost") == {"redacted-session"}
    assert state.capture().pending_for("rollup") == {"redacted-session"}
    for phase in ("cost", "rollup"):
        state.acknowledge(state.capture(), phase)
    refresh_session_usage(conn, {"redacted-session"})
    assert not state.capture().pending_for()


@pytest.mark.parametrize("has_tool", [True, False])
def test_upgrade_duplicate_usage_heals_full_report_rollups_without_new_events(tmp_path, monkeypatch, has_tool):
    from contextlib import closing

    from fastapi.testclient import TestClient

    from reflect.dashboard_server import build_dashboard_app
    from reflect.dashboard_sessions import build_session_payload
    from reflect.preparation_pipeline import prepare_sql_report_db
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.views.sessions import list_sessions

    monkeypatch.setenv("REFLECT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("reflect.pricing._fetch_json_url", lambda *args, **kwargs: {})
    source = tmp_path / "spans"
    source.mkdir()
    request = copy.deepcopy(FIXTURES["agent_stop"])
    request["name"] = request["attributes"]["gen_ai.client.hook.event"] = "Usage"
    request["attributes"].pop("gen_ai.client.agent_id")
    request["attributes"]["gen_ai.request.id"] = "redacted-request"
    second = copy.deepcopy(request)
    second["spanId"] = "redacted-second-observation"
    second["start_time_ns"] += 1_000_000_000
    second["end_time_ns"] += 1_000_000_000
    records = [request, second]
    if has_tool:
        tool = copy.deepcopy(request)
        tool["spanId"] = "redacted-tool"
        tool["name"] = "PostToolUse"
        tool["attributes"] = {"gen_ai.client.name": "claude", "gen_ai.client.session_id": "redacted-session", "gen_ai.client.hook.event": "PostToolUse", "gen_ai.client.tool_name": "Read"}
        records.append(tool)
    write_records(source / "hooks.jsonl", records)
    db = tmp_path / "reflect.db"
    def prepare():
        return prepare_sql_report_db(db, otlp_traces=None, spans_dir=source)

    prepare()
    with closing(connect_sqlite(db)) as conn:
        assert not PendingRefreshState(conn).capture().pending_for()
        # Retained reviewed projection, with no new capture or pending phases.
        conn.execute("UPDATE sessions SET input_tokens=200,output_tokens=100")
        conn.execute("UPDATE session_rollups SET input_tokens=200,output_tokens=100")
        conn.execute("DELETE FROM schema_migrations WHERE version=49")
        conn.commit()
    upgraded = prepare()
    assert upgraded["ingest"]["inserted"] == upgraded["normalize"]["processed"] == 0
    with closing(connect_sqlite(db)) as conn:
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 50)
        assert conn.execute("SELECT input_tokens,output_tokens FROM session_rollups").fetchone() == (100, 50)
        assert list_sessions(conn).rows[0].input_tokens == 100
        assert build_measurements(conn).totals["input_tokens"] == 100
        assert not PendingRefreshState(conn).capture().pending_for()
    payload = build_session_payload(db, "redacted-session")
    assert payload["sqlite"]["sessions"]["rows"][0]["input_tokens"] == 100
    client = TestClient(build_dashboard_app(docs_dir=tmp_path, db_path=db))
    response = client.get("/api/data?range=all")
    assert response.status_code == 200
    assert response.json()["sqlite"]["sessions"]["rows"][0]["input_tokens"] == 100
    repeated = prepare()
    assert repeated["ingest"]["inserted"] == repeated["normalize"]["processed"] == 0
    assert repeated["rollups"]["skipped"] == 1
    with closing(connect_sqlite(db)) as conn:
        assert not PendingRefreshState(conn).capture().pending_for()
        assert conn.execute("SELECT input_tokens,output_tokens FROM session_rollups").fetchone() == (100, 50)
