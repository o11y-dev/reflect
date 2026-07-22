from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from reflect.memory.models import (
    MemoryItem,
    MemoryProviderHealth,
    MemorySearchResult,
    MemoryValidationResult,
)


class OmegaMemoryProvider:
    """Optional adapter over OMEGA's public ``SQLiteStore`` API."""

    name = "omega"

    _TYPE_MAP = {
        "graph_pattern": "lesson_learned",
        "note": "memory",
        "repo_convention": "constraint",
        "workflow_note": "lesson_learned",
    }

    def __init__(
        self,
        home: Path | None = None,
        *,
        store_factory: Callable[..., Any] | None = None,
        module_available: Callable[[], bool] | None = None,
    ) -> None:
        configured_home = os.environ.get("REFLECT_OMEGA_MEMORY_HOME") or os.environ.get("OMEGA_HOME")
        self.home = (home or Path(configured_home or Path.home() / ".omega")).expanduser()
        self.db_path = self.home / "omega.db"
        self._store_factory = store_factory
        self._module_available = module_available or self._default_module_available

    def health(self) -> MemoryProviderHealth:
        if not self._module_available():
            return MemoryProviderHealth(
                self.name,
                False,
                "not_installed",
                'Install the optional integration with `pipx inject o11y-reflect "omega-memory>=1.5,<2"`.',
            )
        if not self.db_path.is_file():
            return MemoryProviderHealth(
                self.name,
                False,
                "not_configured",
                f"OMEGA store not found at {self.db_path}; run `omega setup` explicitly.",
            )
        try:
            with self._open_store() as store:
                count = int(store.node_count())
        except Exception as exc:  # noqa: BLE001 - provider health must return structured failure
            return MemoryProviderHealth(self.name, False, "error", str(exc))
        return MemoryProviderHealth(
            self.name,
            True,
            "ok",
            f"{count} memories in {self.db_path}",
        )

    def remember(self, item: MemoryItem) -> dict[str, Any]:
        source_metadata = item.source_metadata.to_json_dict()
        metadata = {
            "event_type": self._event_type(item.type),
            "project": self._project_for_item(item),
            "source": "reflect",
            "reflect_memory": {
                "id": item.id,
                "type": item.type,
                "scope": item.scope,
                "confidence": item.confidence,
                "sensitivity": item.sensitivity,
                "source_metadata": source_metadata,
            },
        }
        session_id = item.session_id or item.source_metadata.session_id or None
        with self._open_store() as store:
            memory_id = store.store(
                content=item.content,
                session_id=session_id,
                metadata=metadata,
                source_uri=item.source_metadata.source_ref or None,
                sensitivity=item.sensitivity,
            )
        return {"id": str(memory_id), "memory_id": str(memory_id), "provider": self.name}

    def list(
        self,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 100,
    ) -> list[dict]:
        filters = filters or {}
        bounded_limit = max(1, min(int(limit or 100), 500))
        with self._open_store() as store:
            if filters.get("type"):
                rows = store.get_by_type(self._event_type(str(filters["type"])), limit=bounded_limit)
            elif filters.get("session_id"):
                rows = store.get_by_session(str(filters["session_id"]), limit=bounded_limit)
            else:
                rows = store.get_recent(limit=bounded_limit)
        return [
            normalized
            for row in rows
            if (normalized := self._normalize(row))
            and self._matches(normalized, path=path, filters=filters)
        ][:bounded_limit]

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        filters = filters or {}
        bounded_limit = max(1, min(int(limit or 20), 100))
        fetch_limit = min(max(bounded_limit * 3, 20), 300)
        event_type = str(filters.get("type") or "")
        scope = str(filters.get("scope") or "project")
        with self._open_store() as store:
            if event_type:
                rows = store.query_by_type(
                    query,
                    self._event_type(event_type),
                    limit=fetch_limit,
                    project_path=path,
                    scope=scope,
                )
            else:
                rows = store.query(
                    query,
                    limit=fetch_limit,
                    session_id=str(filters.get("session_id") or "") or None,
                    project_path=path,
                    scope=scope if scope in {"project", "session"} else "project",
                )
        results: list[MemorySearchResult] = []
        for row in rows:
            normalized = self._normalize(row)
            if not normalized or not self._matches(normalized, path=path, filters=filters):
                continue
            results.append(
                MemorySearchResult(
                    item=normalized,
                    score=float(getattr(row, "relevance", 0.0) or 0.0),
                    provider=self.name,
                )
            )
            if len(results) >= bounded_limit:
                break
        return results

    def inspect(self, memory_id: str) -> dict | None:
        with self._open_store() as store:
            row = store.get_node(memory_id, track_access=False)
        return self._normalize(row) if row is not None else None

    def forget(self, memory_id: str) -> bool:
        with self._open_store() as store:
            return bool(store.delete_node(memory_id))

    def validate(self, memory_id: str) -> MemoryValidationResult:
        try:
            found = self.inspect(memory_id)
        except Exception as exc:  # noqa: BLE001 - validation reports provider failure
            return MemoryValidationResult(memory_id, "error", error=str(exc))
        if found is None:
            return MemoryValidationResult(memory_id, "stale", stale_reason="provider_memory_missing")
        return MemoryValidationResult(memory_id, "validated")

    @contextmanager
    def _open_store(self) -> Iterator[Any]:
        factory = self._store_factory
        if factory is None:
            from omega import SQLiteStore

            factory = SQLiteStore
        store = factory(db_path=self.db_path)
        try:
            yield store
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _default_module_available() -> bool:
        try:
            return importlib.util.find_spec("omega") is not None
        except (ImportError, ValueError):
            return False

    @classmethod
    def _event_type(cls, reflect_type: str) -> str:
        return cls._TYPE_MAP.get(reflect_type, reflect_type or "memory")

    @staticmethod
    def _project_for_item(item: MemoryItem) -> str:
        source = item.source_metadata
        if source.workspace_root:
            return source.workspace_root
        if source.path:
            return str(Path(source.path).expanduser().parent)
        return ""

    def _normalize(self, row: Any) -> dict[str, Any]:
        metadata = getattr(row, "metadata", {}) or {}
        reflect_metadata = (
            metadata.get("reflect_memory")
            if isinstance(metadata.get("reflect_memory"), dict)
            else {}
        )
        source_metadata = (
            reflect_metadata.get("source_metadata")
            if isinstance(reflect_metadata.get("source_metadata"), dict)
            else {}
        )
        if not source_metadata and metadata.get("project"):
            source_metadata = {"workspace_root": str(metadata["project"])}
        created_at = getattr(row, "created_at", None)
        return {
            "id": str(getattr(row, "id", "") or ""),
            "provider_memory_id": str(getattr(row, "id", "") or ""),
            "content": str(getattr(row, "content", "") or ""),
            "content_preview_redacted": str(getattr(row, "content", "") or "")[:1000],
            "type": str(reflect_metadata.get("type") or metadata.get("event_type") or "memory"),
            "scope": str(reflect_metadata.get("scope") or "project"),
            "provider": self.name,
            "score": float(getattr(row, "relevance", 0.0) or 0.0),
            "created_at": created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at or ""),
            "source_metadata": source_metadata,
            "raw_attrs": {
                "omega_metadata": metadata,
                "access_count": int(getattr(row, "access_count", 0) or 0),
                "strength": float(getattr(row, "strength", 0.0) or 0.0),
            },
        }

    @staticmethod
    def _matches(item: dict[str, Any], *, path: str, filters: dict[str, Any]) -> bool:
        if filters.get("type") and item.get("type") != filters["type"]:
            return False
        if filters.get("scope") and item.get("scope") != filters["scope"]:
            return False
        if not path:
            return True
        metadata = item.get("source_metadata") or {}
        workspace = str(metadata.get("workspace_root") or "")
        source_path = str(metadata.get("path") or "")
        requested = str(Path(path).expanduser().resolve())
        return requested in {workspace, source_path} or source_path.startswith(f"{requested}{os.sep}")
