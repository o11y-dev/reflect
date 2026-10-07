from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from reflect.cli.common import REFLECT_HOME, echo_json, require_snapshot_schema
from reflect.shell_completion import (
    complete_memory_candidate_id,
    complete_memory_id,
    complete_memory_provider,
    complete_memory_scope,
    complete_memory_source,
    complete_memory_type,
    complete_session_id,
)

if TYPE_CHECKING:
    from reflect.memory import MemoryService


@click.group()
def memory() -> None:
    """Evidence-backed local and provider memory commands."""


def _open_memory_service(
    db_path: Path,
    *,
    read_only: bool = False,
) -> tuple[sqlite3.Connection, MemoryService]:
    from reflect.memory import MemoryService
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only

    if read_only:
        require_snapshot_schema(
            db_path,
            refresh_hint=f"Run `reflect memory sync --db-path {db_path}` first.",
        )
        conn = connect_sqlite_read_only(db_path, profile=None)
    else:
        conn = connect_sqlite(db_path)
        migrate(conn)
    return conn, MemoryService(conn, maintain_search_index=not read_only)


def _memory_filters(
    *,
    type: str | None = None,
    scope: str | None = None,
    source: str | None = None,
    provider: str | None = None,
    stale: bool = False,
    validated: bool = False,
    unvalidated: bool = False,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "type": type,
            "scope": scope,
            "source": source,
            "provider": provider,
            "stale": stale,
            "validated": validated,
            "unvalidated": unvalidated,
        }.items()
        if value
    }


@memory.command("providers")
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option("--json", "as_json", is_flag=True, help="Print provider health as JSON.")
def memory_providers(db_path: Path, as_json: bool) -> None:
    """List memory providers and health."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        health = service.provider_health()
    finally:
        conn.close()
    if as_json:
        echo_json(health)
        return
    table = Table(title="Memory Providers")
    table.add_column("Provider")
    table.add_column("Available")
    table.add_column("Status")
    table.add_column("Detail")
    for item in health:
        table.add_row(
            str(item["name"]),
            "yes" if item["available"] else "no",
            str(item["status"]),
            str(item.get("detail") or ""),
        )
    Console().print(table)


@memory.command("sync")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option("--json", "as_json", is_flag=True, help="Print sync result as JSON.")
def memory_sync(path: Path | None, db_path: Path, as_json: bool) -> None:
    """Sync local folder instruction memories. PATH defaults to the current directory."""
    target = path or Path.cwd()
    conn, service = _open_memory_service(db_path)
    try:
        result = service.sync_path(target, home_root=Path.home())
    finally:
        conn.close()
    if as_json:
        echo_json(result)
        return
    click.echo(
        "Synced memories "
        f"(path={target}, discovered={result['discovered']}, inserted={result['inserted']}, updated={result['updated']})"
    )


@memory.command("list")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option(
    "--all", "all_memories", is_flag=True, help="List all memories instead of scoping to PATH."
)
@click.option(
    "--type",
    "memory_type",
    default=None,
    help="Filter by memory type.",
    shell_complete=complete_memory_type,
)
@click.option(
    "--scope", default=None, help="Filter by memory scope.", shell_complete=complete_memory_scope
)
@click.option(
    "--source", default=None, help="Filter by memory source.", shell_complete=complete_memory_source
)
@click.option(
    "--provider", default=None, help="Filter by provider.", shell_complete=complete_memory_provider
)
@click.option("--stale", is_flag=True, help="Only show stale memories.")
@click.option("--validated", is_flag=True, help="Only show validated memories.")
@click.option("--unvalidated", is_flag=True, help="Only show unvalidated memories.")
@click.option("--limit", type=int, default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print memories as JSON.")
def memory_list(
    path: Path | None,
    db_path: Path,
    all_memories: bool,
    memory_type: str | None,
    scope: str | None,
    source: str | None,
    provider: str | None,
    stale: bool,
    validated: bool,
    unvalidated: bool,
    limit: int,
    as_json: bool,
) -> None:
    """List memories for PATH. PATH defaults to the current directory."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        rows = service.list_memories(
            path=path or Path.cwd(),
            all_memories=all_memories,
            filters=_memory_filters(
                type=memory_type,
                scope=scope,
                source=source,
                provider=provider,
                stale=stale,
                validated=validated,
                unvalidated=unvalidated,
            ),
            limit=limit,
        )
    finally:
        conn.close()
    if as_json:
        echo_json(rows)
        return
    table = Table(title="Reflect Memories")
    for column in ("ID", "Type", "Scope", "Source", "Validation", "Path"):
        table.add_column(column)
    for row in rows:
        metadata = row.get("source_metadata") or {}
        raw_attrs = row.get("raw_attrs") or {}
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("type") or ""),
            str(row.get("scope") or ""),
            str(row.get("source") or ""),
            str(row.get("validation_status") or ""),
            str(metadata.get("path") or raw_attrs.get("path") or ""),
        )
    Console().print(table)


@memory.command("search")
@click.argument("query")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option(
    "--type",
    "memory_type",
    default=None,
    help="Filter by memory type.",
    shell_complete=complete_memory_type,
)
@click.option(
    "--scope", default=None, help="Filter by memory scope.", shell_complete=complete_memory_scope
)
@click.option(
    "--provider",
    default="local_sqlite",
    show_default=True,
    help="Provider to search.",
    shell_complete=complete_memory_provider,
)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print search results as JSON.")
def memory_search(
    query: str,
    path: Path | None,
    db_path: Path,
    memory_type: str | None,
    scope: str | None,
    provider: str,
    limit: int,
    as_json: bool,
) -> None:
    """Search memories, optionally scoped to PATH."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        rows = service.search(
            query,
            path=path or Path.cwd(),
            filters=_memory_filters(type=memory_type, scope=scope),
            provider=provider,
            limit=limit,
        )
    finally:
        conn.close()
    if as_json:
        echo_json(rows)
        return
    table = Table(title=f"Memory Search: {query}")
    for column in ("ID", "Type", "Scope", "Provider", "Preview"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("id") or row.get("memory_id") or ""),
            str(row.get("type") or ""),
            str(row.get("scope") or ""),
            str(row.get("provider") or provider),
            str(row.get("content_preview_redacted") or row.get("content") or "")[:100],
        )
    Console().print(table)


@memory.command("inspect")
@click.argument("memory_id", shell_complete=complete_memory_id)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option("--json", "as_json", is_flag=True, help="Print memory as JSON.")
def memory_inspect(memory_id: str, db_path: Path, as_json: bool) -> None:
    """Inspect one memory by ID."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        row = service.inspect(memory_id)
    finally:
        conn.close()
    if row is None:
        raise click.ClickException(f"Memory not found: {memory_id}")
    if as_json:
        echo_json(row)
        return
    Console().print(Panel(json.dumps(row, indent=2, sort_keys=True), title=memory_id))


@memory.command("forget")
@click.argument("memory_id", shell_complete=complete_memory_id)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
def memory_forget(memory_id: str, db_path: Path) -> None:
    """Delete one local memory by ID."""
    conn, service = _open_memory_service(db_path)
    try:
        removed = service.forget(memory_id)
    finally:
        conn.close()
    if not removed:
        raise click.ClickException(f"Memory not found: {memory_id}")
    click.echo(f"Forgot memory {memory_id}")


@memory.command("validate")
@click.argument("memory_id", required=False, shell_complete=complete_memory_id)
@click.option(
    "--candidate",
    "candidate_id",
    default=None,
    help="Promote and validate a graph-derived candidate.",
    shell_complete=complete_memory_candidate_id,
)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option("--json", "as_json", is_flag=True, help="Print validation result as JSON.")
def memory_validate(
    memory_id: str | None, candidate_id: str | None, db_path: Path, as_json: bool
) -> None:
    """Validate a memory or promote a candidate."""
    if not memory_id and not candidate_id:
        raise click.ClickException("Pass MEMORY_ID or --candidate CANDIDATE_ID")
    conn, service = _open_memory_service(db_path)
    try:
        if candidate_id:
            promoted = service.promote_candidate(candidate_id)
            result = service.validate(str(promoted["id"]))
        else:
            result = service.validate(str(memory_id))
    finally:
        conn.close()
    if as_json:
        echo_json(result)
        return
    click.echo(
        f"Memory {result['memory_id']}: {result['status']}"
        + (f" ({result['stale_reason']})" if result.get("stale_reason") else "")
    )


@memory.command("candidates")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option(
    "--session",
    "session_id",
    default="",
    help="Limit candidates to one session ID.",
    shell_complete=complete_session_id,
)
@click.option(
    "--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db"
)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print candidates as JSON.")
def memory_candidates(
    path: Path | None,
    session_id: str,
    db_path: Path,
    limit: int,
    as_json: bool,
) -> None:
    """List graph-derived memory candidates for PATH."""
    conn, service = _open_memory_service(db_path)
    try:
        rows = service.candidates(path=path or Path.cwd(), session_id=session_id, limit=limit)
    finally:
        conn.close()
    if as_json:
        echo_json(rows)
        return
    table = Table(title="Memory Candidates")
    for column in ("ID", "Type", "Confidence", "Content"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("type") or ""),
            f"{float(row.get('confidence') or 0):.2f}",
            str(row.get("content") or "")[:120],
        )
    Console().print(table)
