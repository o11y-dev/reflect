"""Recorded native envelope shapes with explicit accounting/boundary contracts."""
import json

import pytest

from reflect.parsing import (
    _iter_claude_session_spans,
    _iter_codex_session_spans,
    _iter_cursor_session_spans,
    _native_tool_result_metadata,
    _usage_attrs_from_token_count,
)
from reflect.store.ingest import ingest_native_session_file
from reflect.store.migrate import migrate
from reflect.store.normalize import _canonical_usage_attrs, normalize_pending_raw_events
from reflect.store.sqlite import connect_sqlite


def write_records(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n")


@pytest.mark.parametrize("write_key", ["hook.cache_write_tokens", "gen_ai.usage.cache_write.input_tokens"])
def test_cursor_hook_buckets_are_uncached_before_pricing(write_key):
    attrs = {"gen_ai.client.name": "cursor", "gen_ai.usage.input_tokens": 1000, "hook.cache_read_tokens": 600, write_key: 100}
    normalized = _canonical_usage_attrs(attrs)
    assert normalized["gen_ai.usage.input_tokens"] == 300
    assert normalized["reflect.usage.input_semantics"] == "uncached"
    assert normalized["gen_ai.usage.cache_creation.input_tokens"] == 100
    assert attrs["gen_ai.usage.input_tokens"] == 1000
    assert _canonical_usage_attrs(normalized) == normalized
    attrs.pop(write_key)
    missing = _canonical_usage_attrs(attrs)
    assert "gen_ai.usage.input_tokens" not in missing
    assert missing["reflect.usage.unavailable_reason"]
    attrs[write_key] = 500
    assert "gen_ai.usage.input_tokens" not in _canonical_usage_attrs(attrs)


def test_codex_runtime_result_list_preserves_single_process_status():
    result = [
        {"type": "input_text", "text": "Script completed\nWall time 1.5 seconds\nOutput:\n"},
        {"type": "input_text", "text": json.dumps({"chunk_id": "redacted", "wall_time_seconds": 0.1, "exit_code": 1, "output": "CHECK_FAIL"})},
    ]
    assert _native_tool_result_metadata(result)["process.exit.code"] == 1
    # Quoted application JSON and ambiguous multiple results are not envelopes.
    assert "process.exit.code" not in _native_tool_result_metadata(result[1:])
    assert "process.exit.code" not in _native_tool_result_metadata(result + result[1:])


def codex_record(kind, payload, second):
    return {"type": kind, "payload": payload, "timestamp": f"2026-11-01T10:00:{second:02d}Z"}


def test_codex_native_cache_writes_are_preserved_without_double_counting():
    # Recorded Codex 0.166.0 gpt-6.1-sol shape: input includes reads and writes.
    usage = {"input_tokens": 1000, "cached_input_tokens": 600, "cache_write_input_tokens": 200, "output_tokens": 50}
    attrs = _usage_attrs_from_token_count({"last_token_usage": usage})
    assert attrs["gen_ai.usage.input_tokens"] == 200
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 600
    assert attrs["gen_ai.usage.cache_creation.input_tokens"] == 200
    assert attrs["gen_ai.usage.output_tokens"] == 50
    usage.pop("cache_write_input_tokens")
    assert "gen_ai.usage.cache_creation.input_tokens" not in _usage_attrs_from_token_count({"last_token_usage": usage})


def test_codex_cumulative_samples_are_not_additive_requests(tmp_path):
    path = tmp_path / "codex.jsonl"
    records = [codex_record("session_meta", {"id": "codex"}, 0)]
    records.append(codex_record("turn_context", {"turn_id": "t1", "model": "observed-model"}, 1))
    records.append(codex_record("response_item", {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "working"}]}, 2))
    for second, total in enumerate((100, 200, 200, 10, 20), 3):
        records.append(codex_record("event_msg", {"type": "token_count", "info": {"total_token_usage": {"input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}}}, second))
    records.append(codex_record("event_msg", {"type": "task_complete", "turn_id": "t1"}, 9))
    write_records(path, records)
    spans = list(_iter_codex_session_spans(path))
    usage = [span["attributes"] for span in spans if "gen_ai.usage.input_tokens" in span["attributes"]]
    assert sum(row["gen_ai.usage.input_tokens"] for row in usage) == 210
    assert [row["reflect.usage.grain"] for row in usage] == ["session", "interval", "interval"]
    assert all("gen_ai.client.turn_id" not in row for row in usage)
    assert all("gen_ai.request.model" not in row for row in usage)
    assert any(span["attributes"].get("reflect.usage.unavailable_reason") == "counter_reset" for span in spans)
    stops = [span["attributes"] for span in spans if span["attributes"].get("gen_ai.client.hook.event") == "Stop"]
    assert len(stops) == 1
    assert stops[0]["gen_ai.client.turn_id"] == "t1"
    assert stops[0]["gen_ai.request.model"] == "observed-model"


def test_claude_request_updates_and_tool_results_are_reconciled(tmp_path):
    path = tmp_path / "claude.jsonl"
    common = {"sessionId": "claude", "timestamp": "2026-11-01T10:00:00Z"}
    assistant = {**common, "type": "assistant", "message": {"id": "m1", "model": "observed-model", "usage": {"input_tokens": 100, "output_tokens": 2}, "content": [{"type": "tool_use", "id": "tool1", "name": "Bash", "input": {"command": "false"}}]}}
    write_records(path, [
        {**common, "type": "user", "uuid": "prompt1", "message": {"content": [{"type": "text", "text": "check"}]}},
        assistant, assistant,
        {**common, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "tool1", "is_error": True, "content": "exit 1"}]}},
    ])
    spans = list(_iter_claude_session_spans(path))
    attrs = [span["attributes"] for span in spans]
    assert sum(row.get("gen_ai.usage.input_tokens", 0) for row in attrs) == 100
    assert sum(row["gen_ai.client.hook.event"] == "UserPromptSubmit" for row in attrs) == 1
    assert sum(row["gen_ai.client.hook.event"] == "PreToolUse" for row in attrs) == 1
    result = next(row for row in attrs if row["gen_ai.client.hook.event"] == "PostToolUseFailure")
    assert result["gen_ai.tool.call.id"] == "tool1"
    assert result["gen_ai.client.tool_name"] == "Bash"
    assert not any(row["gen_ai.client.hook.event"] == "Stop" for row in attrs)
    usage = next(row for row in attrs if "gen_ai.usage.input_tokens" in row)
    assert "gen_ai.usage.cache_read.input_tokens" not in usage


def test_cursor_inferred_time_and_identity_are_explicit(tmp_path):
    path = tmp_path / "cursor.jsonl"
    write_records(path, [{"role": "assistant", "message": {"content": [{"type": "tool_use", "name": "Shell", "input": {"command": "pwd"}}]}}])
    attrs = [span["attributes"] for span in _iter_cursor_session_spans(path)]
    assert all(row["reflect.time.provenance"] == "inferred" for row in attrs)
    assert not any(row["gen_ai.client.hook.event"] == "Stop" for row in attrs)
    tool = next(row for row in attrs if row["gen_ai.client.hook.event"] == "PreToolUse")
    assert tool["reflect.tool.identity_provenance"] == "synthetic"


def test_claude_provider_retry_is_observed_without_task_completion(tmp_path):
    path = tmp_path / "claude.jsonl"
    write_records(path, [{"sessionId": "failed-provider", "type": "system", "subtype": "api_error", "timestamp": "2026-11-04T10:00:00Z", "retryAttempt": 2, "error": {"message": "Connection error.", "formatted": "Connection dropped (ECONNRESET)", "connection": {"code": "ECONNRESET"}}}])
    attrs = [span["attributes"] for span in _iter_claude_session_spans(path)]
    error = next(row for row in attrs if row["gen_ai.client.hook.event"] == "ApiError")
    assert error["error.type"] == "ECONNRESET"
    assert error["reflect.provider.retry_attempt"] == 2
    assert not any(row["gen_ai.client.hook.event"] == "Stop" for row in attrs)


def test_changed_native_projection_replays_without_duplicate_usage(tmp_path):
    path = tmp_path / "claude.jsonl"
    record = {"type": "assistant", "sessionId": "claude", "timestamp": "2026-11-01T10:00:00Z", "message": {"id": "m1", "model": "observed-model", "usage": {"input_tokens": 100, "output_tokens": 2}, "content": []}}
    write_records(path, [record])
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 100
        record["message"]["usage"]["output_tokens"] = 5
        write_records(path, [record, record])
        replay = ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert replay["replaced_native_events"] == 1
        assert conn.execute("SELECT normalized_status FROM raw_events WHERE event_type='gen_ai.client.hook.SessionStart'").fetchone()[0] == "ok"
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT SUM(input_tokens), SUM(output_tokens) FROM llm_calls").fetchone() == (100, 5)
        assert conn.execute("SELECT input_tokens, output_tokens FROM sessions").fetchone() == (100, 5)
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status = 'superseded'").fetchone()[0] > 0
        again = ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert again == {"inserted": 0, "skipped": 0, "unchanged": 1}
    finally:
        conn.close()


def test_codex_append_preserves_normalized_usage_and_only_queues_tail(tmp_path):
    path = tmp_path / "codex.jsonl"
    usage = {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 2}
    records = [
        codex_record("session_meta", {"id": "codex-append"}, 0),
        codex_record("turn_context", {"turn_id": "turn", "model": "observed-model"}, 1),
        codex_record("event_msg", {"type": "token_count", "info": {"last_token_usage": usage}}, 2),
    ]
    write_records(path, records)
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="codex", skip_unchanged=True)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        old_raw_id = conn.execute("SELECT id FROM raw_events WHERE event_type = 'gen_ai.client.hook.Usage'").fetchone()[0]
        old_call = conn.execute("SELECT id,step_id,input_tokens,output_tokens FROM llm_calls").fetchone()
        records.append(codex_record("event_msg", {"type": "token_count", "info": {"last_token_usage": usage}}, 3))
        write_records(path, records)
        result = ingest_native_session_file(conn, file_path=path, agent="codex", skip_unchanged=True)
        assert conn.execute("SELECT normalized_status FROM raw_events WHERE id = ?", (old_raw_id,)).fetchone()[0] == "ok"
        assert conn.execute("SELECT id,step_id,input_tokens,output_tokens FROM llm_calls").fetchone() == old_call
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 2)
        pending = conn.execute("SELECT event_type,COUNT(*) FROM raw_events WHERE normalized_status='pending' GROUP BY event_type").fetchall()
        assert dict(pending).get("gen_ai.client.hook.Usage") == 1
        assert result["inserted"] <= 3  # New sample and changed synthetic extents only.
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (200, 4)
    finally:
        conn.close()


def test_native_append_during_decode_is_not_checkpointed_unseen(tmp_path, monkeypatch):
    from reflect.store import ingest

    path = tmp_path / "codex.jsonl"
    usage = {"input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0, "output_tokens": 2}
    records = [
        codex_record("session_meta", {"id": "append-race"}, 0),
        codex_record("event_msg", {"type": "token_count", "info": {"last_token_usage": usage}}, 1),
    ]
    write_records(path, records)
    decoded_size = path.stat().st_size
    parser = ingest._iter_codex_session_spans

    def append_after_decode(source):
        yield from parser(source)
        with source.open("a") as handle:
            handle.write(json.dumps(codex_record("event_msg", {"type": "token_count", "info": {"last_token_usage": usage}}, 2)) + "\n")

    monkeypatch.setattr(ingest, "_iter_codex_session_spans", append_after_decode)
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="codex", skip_unchanged=True)
        assert conn.execute("SELECT size_bytes FROM source_ingestion_state").fetchone()[0] == decoded_size
        assert path.stat().st_size > decoded_size
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 100
        monkeypatch.setattr(ingest, "_iter_codex_session_spans", parser)
        result = ingest_native_session_file(conn, file_path=path, agent="codex", skip_unchanged=True)
        assert not result["unchanged"]
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 200
    finally:
        conn.close()


def test_native_decoder_upgrade_still_replays_all_retained_rows(tmp_path):
    path = tmp_path / "claude.jsonl"
    record = {"type": "assistant", "sessionId": "upgrade", "timestamp": "2026-11-01T10:00:00Z", "message": {"id": "m1", "usage": {"input_tokens": 100, "output_tokens": 2}, "content": []}}
    write_records(path, [record])
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        previous = conn.execute("SELECT id,input_tokens,output_tokens FROM llm_calls").fetchall()
        count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        conn.execute("UPDATE source_ingestion_state SET decoder_version=2")
        result = ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert result["replaced_native_events"] == count
        assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE normalized_status='pending'").fetchone()[0] == count
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT id,input_tokens,output_tokens FROM llm_calls").fetchall() == previous
    finally:
        conn.close()


@pytest.mark.parametrize("has_timestamp", [True, False], ids=["boundary", "request-only"])
def test_native_usage_retraction_requests_scoped_derived_refresh(tmp_path, has_timestamp):
    path = tmp_path / "claude.jsonl"
    def record(request_id, tokens):
        return {"type": "assistant", "sessionId": "retract", **({"timestamp": "2026-11-01T10:00:00Z"} if has_timestamp else {}), "message": {"id": request_id, "usage": {"input_tokens": tokens, "output_tokens": 2}, "content": []}}
    records = [record("m1", 100), record("m2", 50)]
    write_records(path, records)
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        if not has_timestamp:
            assert conn.execute("SELECT COUNT(*) FROM raw_events WHERE event_type='gen_ai.client.hook.SessionStart'").fetchone()[0] == 0
        retained_call = conn.execute("SELECT id FROM llm_calls WHERE input_tokens=100").fetchone()[0]
        write_records(path, records[:1])
        result = ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert result["inserted"] == 0 and result["replaced_native_events"] == 1
        pending = conn.execute("SELECT event_type FROM raw_events WHERE normalized_status='pending'").fetchall()
        assert len(pending) == 1
        if has_timestamp:
            assert pending == [("gen_ai.client.hook.SessionStart",)]
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 100
        changed = set()
        assert normalize_pending_raw_events(conn, changed_session_ids=changed)["failed"] == 0
        assert changed == {"retract"}
        assert conn.execute("SELECT input_tokens FROM sessions").fetchone()[0] == 100
        assert conn.execute("SELECT id,input_tokens,output_tokens FROM llm_calls").fetchall() == [(retained_call, 100, 2)]
        assert ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)["unchanged"] == 1
        assert normalize_pending_raw_events(conn)["processed"] == 0
        assert conn.execute("SELECT input_tokens,output_tokens FROM sessions").fetchone() == (100, 2)
    finally:
        conn.close()


@pytest.mark.parametrize("replacement", ["success", "removed"])
def test_native_result_retraction_rebuilds_only_affected_tool_pair(tmp_path, replacement):
    path = tmp_path / "claude.jsonl"
    common = {"sessionId": "tool-retraction", "timestamp": "2026-11-01T10:00:00Z"}
    request = {**common, "type": "assistant", "message": {"id": "m1", "content": [{"type": "tool_use", "id": "call1", "name": "Bash", "input": {"command": "python3 verify.py"}}]}}
    result = {**common, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "call1", "is_error": True, "content": "old failure"}]}}
    write_records(path, [request, result])
    conn = connect_sqlite(tmp_path / "reflect.db")
    try:
        migrate(conn)
        ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert normalize_pending_raw_events(conn)["failed"] == 0
        assert conn.execute("SELECT status FROM tool_calls").fetchone()[0] == "error"
        invocation_id = conn.execute("SELECT id FROM raw_events WHERE event_type='gen_ai.client.hook.PreToolUse'").fetchone()[0]
        if replacement == "success":
            result["message"]["content"][0].update(is_error=False, content="new success")
        write_records(path, [request, result] if replacement == "success" else [request])
        replay = ingest_native_session_file(conn, file_path=path, agent="claude", skip_unchanged=True)
        assert replay["replaced_native_events"] == 1
        assert conn.execute("SELECT normalized_status FROM raw_events WHERE id=?", (invocation_id,)).fetchone()[0] == "pending"
        assert normalize_pending_raw_events(conn)["failed"] == 0
        status, output = conn.execute("SELECT status,output_preview_redacted FROM tool_calls").fetchone()
        assert status != "error"
        assert output == ("new success" if replacement == "success" else None)
    finally:
        conn.close()
