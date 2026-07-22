import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from reflect.context import ReflectContextService
from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _seed_memory(db_path, workspace):
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        remembered = MemoryService(conn).remember(
            MemoryItem(
                content="Run the release gate before publishing.",
                type="repo_convention",
                scope="project",
                source_metadata=MemorySourceMetadata(
                    source_kind="manual",
                    source_ref="manual",
                    path=str(workspace / "AGENTS.md"),
                    workspace_root=str(workspace),
                    manual_note=True,
                ),
                confidence=0.9,
            )
        )
        return remembered["id"]
    finally:
        conn.close()


def test_context_service_combines_memory_with_guidance(tmp_path):
    db_path = tmp_path / "reflect.db"
    memory_id = _seed_memory(db_path, tmp_path)
    conn = connect_sqlite(db_path)
    try:
        answer = ReflectContextService(conn).ask("release gate", path=tmp_path)
    finally:
        conn.close()

    assert answer.memories[0].id == memory_id
    assert answer.memories[0].provenance == "local_memory"
    assert answer.evidence == []
    assert "no matching approved workflow" in answer.answer


def test_reflect_mcp_supports_initialize_list_and_call(tmp_path):
    db_path = tmp_path / "reflect.db"
    memory_id = _seed_memory(db_path, tmp_path)

    async def exercise_server():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "reflect.mcp"],
            env={**os.environ, "REFLECT_DB_PATH": str(db_path)},
        )
        async with (
            stdio_client(params) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as session,
        ):
            initialized = await session.initialize()
            tools = await session.list_tools()
            discovered = {tool.name: tool for tool in tools.tools}
            result = await session.call_tool(
                "reflect_context",
                {"question": "release gate", "path": str(tmp_path)},
            )
            return initialized, discovered, result

    initialized, discovered, result = asyncio.run(exercise_server())
    names = set(discovered)

    assert initialized.serverInfo.name == "Reflect"
    assert names == {
        "reflect_context",
        "reflect_explain",
        "reflect_improvements",
        "reflect_usage",
    }
    assert not result.isError
    assert result.structuredContent is not None
    assert result.structuredContent["memories"][0]["id"] == memory_id
    assert not {"memory_search", "memory_remember", "memory_validate"} & names
    assert all(tool.annotations and tool.annotations.readOnlyHint for tool in discovered.values())
    assert all(tool.annotations and not tool.annotations.destructiveHint for tool in discovered.values())
