from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from reflect.core import REFLECT_HOME
from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def _service(db_path: str | None = None) -> tuple[Any, MemoryService]:
    conn = connect_sqlite(Path(db_path) if db_path else REFLECT_HOME / "state" / "reflect.db")
    migrate(conn)
    return conn, MemoryService(conn)


def memory_search(arguments: dict[str, Any]) -> dict[str, Any]:
    conn, service = _service(arguments.get("db_path"))
    try:
        results = service.search(
            str(arguments.get("query") or ""),
            path=Path(arguments["path"]) if arguments.get("path") else None,
            provider=str(arguments.get("provider") or "local_sqlite"),
            limit=int(arguments.get("limit") or 20),
        )
    finally:
        conn.close()
    return {"results": results}


def memory_remember(arguments: dict[str, Any]) -> dict[str, Any]:
    source = _source_from_arguments(arguments)
    item = MemoryItem(
        content=str(arguments.get("content") or ""),
        type=str(arguments.get("type") or "note"),
        scope=str(arguments.get("scope") or "project"),
        source_metadata=source,
        confidence=float(arguments.get("confidence") or 0.5),
        sensitivity=str(arguments.get("sensitivity") or "unknown"),
    )
    conn, service = _service(arguments.get("db_path"))
    try:
        remembered = service.remember(
            item,
            semantic_domain=str(arguments.get("semantic_domain") or "reflect_operational"),
            provider=arguments.get("provider"),
        )
    finally:
        conn.close()
    return {"memory": remembered}


def memory_validate(arguments: dict[str, Any]) -> dict[str, Any]:
    conn, service = _service(arguments.get("db_path"))
    try:
        if arguments.get("candidate_id"):
            remembered = service.promote_candidate(str(arguments["candidate_id"]))
            result = service.validate(str(remembered["id"]))
        else:
            result = service.validate(str(arguments.get("memory_id") or ""))
    finally:
        conn.close()
    return {"validation": result}


def service_context(arguments: dict[str, Any]) -> dict[str, Any]:
    conn, service = _service(arguments.get("db_path"))
    try:
        providers = service.provider_health()
        memories = service.list_memories(
            path=Path(arguments["path"]) if arguments.get("path") else Path.cwd(),
            all_memories=bool(arguments.get("all")),
            limit=int(arguments.get("limit") or 20),
        )
    finally:
        conn.close()
    return {"providers": providers, "memories": memories}


def explain(arguments: dict[str, Any]) -> dict[str, Any]:
    conn, service = _service(arguments.get("db_path"))
    try:
        memory = service.inspect(str(arguments.get("memory_id") or ""))
    finally:
        conn.close()
    if memory is None:
        return {"found": False, "reason": "memory_not_found"}
    return {
        "found": True,
        "memory_id": memory["id"],
        "provider": memory.get("provider"),
        "source_metadata": memory.get("source_metadata"),
        "raw_attrs": memory.get("raw_attrs"),
        "confidence": memory.get("confidence"),
        "validation_status": memory.get("validation_status"),
        "stale_reason": memory.get("stale_reason") or "",
    }


TOOLS = {
    "memory_search": memory_search,
    "memory_remember": memory_remember,
    "memory_validate": memory_validate,
    "service_context": service_context,
    "explain": explain,
}


def main() -> None:
    """Minimal JSON-lines stdio tool loop for local MCP-style clients."""
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            name = str(request.get("tool") or request.get("name") or "")
            arguments = request.get("arguments") if isinstance(request.get("arguments"), dict) else {}
            if name not in TOOLS:
                raise KeyError(f"Unknown tool: {name}")
            payload = {"ok": True, "result": TOOLS[name](arguments)}
        except Exception as exc:  # noqa: BLE001 - stdio server returns structured errors
            payload = {"ok": False, "error": str(exc)}
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        sys.stdout.flush()


def _source_from_arguments(arguments: dict[str, Any]) -> MemorySourceMetadata:
    if arguments.get("manual_note"):
        return MemorySourceMetadata.manual()
    return MemorySourceMetadata(
        source_kind=str(arguments.get("source_kind") or ""),
        source_ref=str(arguments.get("source_ref") or ""),
        path=str(arguments.get("path") or ""),
        workspace_root=str(arguments.get("workspace_root") or ""),
        session_id=str(arguments.get("session_id") or ""),
        step_id=str(arguments.get("step_id") or ""),
        repo_id=str(arguments.get("repo_id") or ""),
        file_id=str(arguments.get("file_id") or ""),
        spec_id=str(arguments.get("spec_id") or ""),
        content_hash=str(arguments.get("content_hash") or ""),
        attrs=arguments.get("attrs") if isinstance(arguments.get("attrs"), dict) else {},
    )
