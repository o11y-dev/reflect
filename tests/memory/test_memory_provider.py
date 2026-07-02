from unittest.mock import patch

import pytest

from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata, MemoryValidationError
from reflect.memory.registry import LiteLLMMemoryProvider
from reflect.store.graph_normalize import rebuild_graph
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _service(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    migrate(conn)
    return conn, MemoryService(conn)


def test_memory_requires_source_metadata_unless_manual(tmp_path):
    conn, service = _service(tmp_path)
    try:
        with pytest.raises(MemoryValidationError):
            service.remember(
                MemoryItem(
                    content="Use provider metadata.",
                    type="repo_convention",
                    scope="project",
                    source_metadata=MemorySourceMetadata(source_kind="", source_ref=""),
                )
            )

        row = service.remember(
            MemoryItem(
                content="Manual reminder",
                type="note",
                scope="project",
                source_metadata=MemorySourceMetadata.manual(),
            )
        )
        assert row["source"] == "manual"
        assert row["validation_status"] == "validated"
    finally:
        conn.close()


def test_sqlite_memory_search_inspect_forget_and_validate(tmp_path):
    conn, service = _service(tmp_path)
    source_file = tmp_path / "repo" / "AGENTS.md"
    source_file.parent.mkdir()
    source_file.write_text("Memory provider rules\n", encoding="utf-8")
    try:
        result = service.sync_path(source_file.parent, home_root=tmp_path / "home")
        assert result["inserted"] == 1

        rows = service.search("provider", path=source_file.parent)
        assert rows
        memory_id = rows[0]["id"]
        inspected = service.inspect(memory_id)
        assert inspected["source_metadata"]["path"] == str(source_file)

        validation = service.validate(memory_id)
        assert validation["status"] == "validated"

        assert service.forget(memory_id) is True
        assert service.inspect(memory_id) is None
    finally:
        conn.close()


def test_stale_memory_detection_for_deleted_source(tmp_path):
    conn, service = _service(tmp_path)
    source_file = tmp_path / "repo" / "AGENTS.md"
    source_file.parent.mkdir()
    source_file.write_text("Memory provider rules\n", encoding="utf-8")
    try:
        service.sync_path(source_file.parent, home_root=tmp_path / "home")
        memory_id = service.list_memories(path=source_file.parent)[0]["id"]
        source_file.unlink()

        validation = service.validate(memory_id)

        assert validation["status"] == "stale"
        assert validation["stale_reason"] == "source_path_missing"
    finally:
        conn.close()


def test_provider_discovery_reports_external_stubs(tmp_path):
    conn, service = _service(tmp_path)
    try:
        health = {item["name"]: item for item in service.provider_health()}
    finally:
        conn.close()

    assert health["local_sqlite"]["available"] is True
    assert health["agentmemory"]["available"] is False
    assert health["litellm"]["status"] == "not_configured"
    assert health["memorypalace"]["status"] == "not_configured"
    assert health["mem0"]["status"] == "not_configured"
    assert health["graphiti"]["status"] == "not_configured"
    assert health["tencentdb_agent_memory"]["status"] == "not_configured"


def test_agentmemory_routing_falls_back_to_local_sqlite(tmp_path):
    conn, service = _service(tmp_path)
    try:
        with patch.dict("os.environ", {"AGENTMEMORY_URL": "http://127.0.0.1:9"}):
            row = service.remember(
                MemoryItem(
                    content="Generic agent memory",
                    type="workflow_note",
                    scope="session",
                    source_metadata=MemorySourceMetadata(
                        source_kind="session",
                        source_ref="session://abc",
                        session_id="abc",
                    ),
                ),
                semantic_domain="generic_agent_session",
            )

        assert row["provider"] == "agentmemory"
        assert row["provider_status"] == "local_fallback"
    finally:
        conn.close()


class _FakeHTTPResponse:
    def __init__(self, payload: str, status: int = 200):
        self.payload = payload.encode("utf-8")
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def test_litellm_memory_provider_uses_v1_memory_contract():
    calls = []

    def fake_urlopen(request, timeout):
        calls.append((request.full_url, request.get_method(), request.data, timeout))
        if request.get_method() == "GET":
            return _FakeHTTPResponse('{"memories": [], "total": 0}')
        return _FakeHTTPResponse(
            '{"memory_id": "remote-1", "key": "reflect:project:repo_convention:abc", "value": "Keep docs current."}'
        )

    provider = LiteLLMMemoryProvider("http://litellm.local", "sk-test")
    item = MemoryItem(
        id="abc",
        content="Keep docs current.",
        type="repo_convention",
        scope="project",
        source_metadata=MemorySourceMetadata.manual(),
    )

    with patch("reflect.memory.registry.urllib_request.urlopen", fake_urlopen):
        assert provider.health().available is True
        result = provider.remember(item)

    assert result["memory_id"] == "remote-1"
    assert calls[0][0] == "http://litellm.local/v1/memory?page_size=1"
    assert calls[0][1] == "GET"
    assert calls[1][0] == "http://litellm.local/v1/memory/reflect%3Aproject%3Arepo_convention%3Aabc"
    assert calls[1][1] == "PUT"
    assert b'"value": "Keep docs current."' in calls[1][2]


def test_litellm_routing_mirrors_remote_memory_locally(tmp_path):
    conn, service = _service(tmp_path)

    def fake_urlopen(request, timeout):
        if request.get_method() == "GET":
            return _FakeHTTPResponse('{"memories": [], "total": 0}')
        return _FakeHTTPResponse(
            '{"memory_id": "remote-1", "key": "reflect:session:workflow_note:abc", "value": "Use LiteLLM memory."}'
        )

    try:
        with (
            patch.dict("os.environ", {"LITELLM_MEMORY_URL": "http://litellm.local", "LITELLM_API_KEY": "sk-test"}),
            patch("reflect.memory.registry.urllib_request.urlopen", fake_urlopen),
        ):
            row = service.remember(
                MemoryItem(
                    id="abc",
                    content="Use LiteLLM memory.",
                    type="workflow_note",
                    scope="session",
                    source_metadata=MemorySourceMetadata.manual(),
                ),
                provider="litellm",
            )

        assert row["provider"] == "litellm"
        assert row["provider_status"] == "mirrored"
        assert row["provider_memory_id"] == "remote-1"
    finally:
        conn.close()


def test_generic_agent_session_routes_to_litellm_when_configured(tmp_path):
    conn, service = _service(tmp_path)

    def fake_urlopen(request, timeout):
        if request.get_method() == "GET":
            return _FakeHTTPResponse('{"memories": [], "total": 0}')
        return _FakeHTTPResponse(
            '{"memory_id": "remote-2", "key": "reflect:session:workflow_note:abc", "value": "Route generic memory."}'
        )

    try:
        with (
            patch.dict("os.environ", {"LITELLM_MEMORY_URL": "http://litellm.local", "LITELLM_API_KEY": "sk-test"}),
            patch("reflect.memory.registry.urllib_request.urlopen", fake_urlopen),
        ):
            row = service.remember(
                MemoryItem(
                    id="abc",
                    content="Route generic memory.",
                    type="workflow_note",
                    scope="session",
                    source_metadata=MemorySourceMetadata.manual(),
                ),
                semantic_domain="generic_agent_session",
            )

        assert row["provider"] == "litellm"
        assert row["provider_status"] == "mirrored"
        assert row["provider_memory_id"] == "remote-2"
    finally:
        conn.close()


def test_graph_candidates_promote_to_memory(tmp_path):
    conn, service = _service(tmp_path)
    try:
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO agents(id, name, raw_json, created_at, updated_at) VALUES ('a', 'codex', '{}', ?, ?)",
            (now, now),
        )
        for session_id in ("sess-a", "sess-b"):
            conn.execute(
                """
                INSERT INTO sessions(id, agent_id, started_at, status, created_at, updated_at)
                VALUES (?, 'a', ?, 'ok', ?, ?)
                """,
                (session_id, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO steps(id, session_id, seq, type, started_at, status, raw_attrs_json, created_at, updated_at)
                VALUES (?, ?, 1, 'tool_call', ?, 'ok', '{}', ?, ?)
                """,
                (f"step-{session_id}", session_id, now, now, now),
            )
            conn.execute(
                """
                INSERT INTO tool_calls(
                  id, step_id, session_id, tool_name, status, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, 'Read', 'ok', '{}', ?, ?)
                """,
                (f"tool-{session_id}", f"step-{session_id}", session_id, now, now),
            )
        conn.commit()
        rebuild_graph(conn)

        candidates = service.candidates(path=tmp_path)
        read_candidate = next(item for item in candidates if "Read" in item["content"])
        remembered = service.promote_candidate(read_candidate["id"])

        assert remembered["type"] == "graph_pattern"
        assert remembered["source"] == "graph_candidate"
    finally:
        conn.close()
