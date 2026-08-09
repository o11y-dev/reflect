from __future__ import annotations

import hashlib
import json
from pathlib import Path

from reflect import core
from reflect.context import ReflectContextService
from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

from .harness import AGENT_ADAPTERS, blog_memory_content


def _span(
    *,
    agent: str,
    session_id: str,
    name: str,
    span_id: str,
    timestamp_ns: int,
    attrs: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "traceId": f"trace-{session_id}",
        "spanId": span_id,
        "parentSpanId": "",
        "start_time_ns": timestamp_ns,
        "end_time_ns": timestamp_ns + 1_000_000,
        "attributes": {
            "gen_ai.client.name": agent,
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            **(attrs or {}),
        },
    }


def _write_session_spans(
    path: Path,
    *,
    agent: str,
    session_id: str,
    timestamp_ns: int,
) -> None:
    spans = [
        _span(
            agent=agent,
            session_id=session_id,
            name="UserPromptSubmit",
            span_id=f"{session_id}-prompt",
            timestamp_ns=timestamp_ns,
            attrs={
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.prompt": "Reflect blog contract",
            },
        )
    ]
    for offset, tool_name in enumerate(("reflect_context", "reflect_complete"), start=1):
        call_id = f"{session_id}:{tool_name}"
        common = {
            "gen_ai.tool.call.id": call_id,
            "gen_ai.client.mcp_server": "reflect",
            "gen_ai.client.mcp_tool": tool_name,
        }
        spans.extend(
            [
                _span(
                    agent=agent,
                    session_id=session_id,
                    name="BeforeMCPExecution",
                    span_id=f"{session_id}-{offset}-start",
                    timestamp_ns=timestamp_ns + offset * 10_000_000,
                    attrs={**common, "gen_ai.client.hook.event": "BeforeMCPExecution"},
                ),
                _span(
                    agent=agent,
                    session_id=session_id,
                    name="AfterMCPExecution",
                    span_id=f"{session_id}-{offset}-end",
                    timestamp_ns=timestamp_ns + offset * 10_000_000 + 2_000_000,
                    attrs={
                        **common,
                        "gen_ai.client.hook.event": "AfterMCPExecution",
                        "gen_ai.client.status": "ok",
                    },
                ),
            ]
        )
    spans.append(
        _span(
            agent=agent,
            session_id=session_id,
            name="Stop",
            span_id=f"{session_id}-stop",
            timestamp_ns=timestamp_ns + 40_000_000,
            attrs={
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.request.model": f"test-{agent}",
                "gen_ai.usage.input_tokens": 100,
                "gen_ai.usage.output_tokens": 20,
                "gen_ai.client.output": "Validated blog output",
            },
        )
    )
    path.write_text(
        "\n".join(json.dumps(span) for span in spans) + "\n",
        encoding="utf-8",
    )


def test_cross_agent_blog_contract_has_exact_idempotent_telemetry(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "reflect.db"
    spans_dir = tmp_path / "local-spans"
    spans_dir.mkdir()
    memory_workspace = tmp_path / "memory-workspace"
    memory_workspace.mkdir()
    source_path = memory_workspace / "AGENTS.md"
    source_path.write_text(blog_memory_content(), encoding="utf-8")

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        memory_content = blog_memory_content()
        memory_service = MemoryService(conn)
        memory = memory_service.remember(
            MemoryItem(
                content=memory_content,
                type="agent_instruction",
                scope="project",
                confidence=1.0,
                source_metadata=MemorySourceMetadata.from_path(
                    source_path,
                    workspace_root=memory_workspace,
                    source_kind="fresh_install_fixture",
                    content_hash=hashlib.sha256(memory_content.encode()).hexdigest(),
                ),
            )
        )
        memory_id = str(memory["id"])
        assert memory_service.validate(memory_id)["status"] == "validated"
        service = ReflectContextService(conn)
        timestamp_ns = 1_786_000_000_000_000_000
        expected_sessions: list[str] = []
        for stage in ("draft", "revision"):
            for adapter in AGENT_ADAPTERS:
                session_id = f"blog-{stage}-{adapter.name}"
                expected_sessions.append(session_id)
                monkeypatch.setenv("REFLECT_SESSION_ID", session_id)
                started = service.begin_task(
                    f"Reflect blog {stage} stage contract",
                    path=memory_workspace,
                )
                assert [item.id for item in started.memories] == [memory_id]
                service.complete_task(
                    str(started.task_run_id),
                    outcome="success",
                    verification_passed=True,
                    summary_redacted=f"blog-{stage}:{adapter.name}",
                )
                _write_session_spans(
                    spans_dir / f"{session_id}.jsonl",
                    agent=adapter.name,
                    session_id=session_id,
                    timestamp_ns=timestamp_ns + (len(expected_sessions) * 1_000_000_000),
                )
    finally:
        conn.close()

    first = core.prepare_sql_report_db(
        db_path,
        otlp_traces=None,
        include_native_sessions=False,
        spans_dir=spans_dir,
    )
    second = core.prepare_sql_report_db(
        db_path,
        otlp_traces=None,
        include_native_sessions=False,
        spans_dir=spans_dir,
    )

    conn = connect_sqlite(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM mcp_task_runs").fetchone()[0] == 12
        assert conn.execute(
            "SELECT COUNT(*) FROM mcp_task_runs WHERE status = 'completed' AND session_linked_at IS NOT NULL"
        ).fetchone()[0] == 12
        assert conn.execute(
            "SELECT SUM(memory_exposure_recorded_count) FROM mcp_task_runs"
        ).fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM memory_exposures").fetchone()[0] == 12
        assert conn.execute("SELECT COUNT(*) FROM mcp_calls").fetchone()[0] == 24
        assert conn.execute(
            """
            SELECT COUNT(*)
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            WHERE tc.status = 'ok'
            """
        ).fetchone()[0] == 24
        assert conn.execute(
            """
            SELECT COUNT(DISTINCT tc.session_id || ':' || mc.tool_call_id)
            FROM mcp_calls AS mc
            JOIN tool_calls AS tc ON tc.id = mc.tool_call_id
            """
        ).fetchone()[0] == 24
        assert conn.execute("SELECT SUM(input_tokens + output_tokens) FROM sessions").fetchone()[0] == 1_440
        assert {
            row[0] for row in conn.execute("SELECT DISTINCT name FROM agents")
        } == {adapter.name for adapter in AGENT_ADAPTERS}
    finally:
        conn.close()

    assert first["ingest_sources"]["local_spans"]["files"] == 12
    assert first["ingest_sources"]["local_spans"]["inserted"] == 72
    assert second["ingest_sources"]["local_spans"]["unchanged"] == 12
    assert second["ingest_sources"]["local_spans"]["inserted"] == 0
