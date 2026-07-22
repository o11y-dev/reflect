from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from reflect.context import ReflectContextService
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite

SERVER_INSTRUCTIONS = """
Reflect provides local telemetry evidence, reviewed workflows, scoped context, and exact usage.
Use reflect_context before recurring repository work. Treat provider memory as context rather than
Reflect-verified evidence, and never apply pending workflows without explicit operator approval.
""".strip()

mcp = FastMCP("Reflect", instructions=SERVER_INSTRUCTIONS)
ResultT = TypeVar("ResultT")
READ_ONLY_TOOL = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _db_path() -> Path:
    explicit = os.environ.get("REFLECT_DB_PATH", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    home = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")).expanduser()
    return home / "state" / "reflect.db"


def _with_service(operation: Callable[[ReflectContextService], ResultT]) -> ResultT:
    conn = connect_sqlite(_db_path())
    try:
        migrate(conn)
        return operation(ReflectContextService(conn))
    finally:
        conn.close()


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_context(
    question: str,
    path: str = "",
    task_file: str = "",
    memory_provider: str = "local_sqlite",
    memory_limit: int = 5,
) -> dict[str, Any]:
    """Get task guidance from approved workflows, local evidence, and scoped memory."""

    resolved_path = Path(path).expanduser().resolve() if path else Path.cwd()
    resolved_task = Path(task_file).expanduser().resolve() if task_file else None
    return _with_service(
        lambda service: service.ask(
            question,
            task_file=resolved_task,
            path=resolved_path,
            memory_provider=memory_provider,
            memory_limit=memory_limit,
        ).model_dump(mode="json")
    )


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_improvements(limit: int = 20) -> dict[str, Any]:
    """List current evidence-backed findings without running detectors or applying changes."""

    return _with_service(lambda service: service.improvements_summary(limit=limit))


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_explain(entity_id: str) -> dict[str, Any]:
    """Explain one Reflect observation, workflow, or local memory with provenance."""

    return _with_service(lambda service: service.explain(entity_id))


@mcp.tool(annotations=READ_ONLY_TOOL)
def reflect_usage(
    session_id: str = "",
    global_scope: bool = False,
    period: str = "week",
    agent: str = "",
) -> dict[str, Any]:
    """Return exact local usage for one session or a global day, week, month, or all-time scope."""

    return _with_service(
        lambda service: service.usage_report(
            session_id=session_id or None,
            global_scope=global_scope,
            period=period,
            agent=agent or None,
        )
    )


def main() -> None:
    """Run Reflect's standards-compliant local MCP server over stdio."""

    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
