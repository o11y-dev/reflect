from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class MemoryValidationError(ValueError):
    """Raised when a memory write does not meet Reflect evidence rules."""


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


@dataclass(frozen=True)
class MemorySourceMetadata:
    source_kind: str
    source_ref: str
    path: str = ""
    workspace_root: str = ""
    session_id: str = ""
    step_id: str = ""
    repo_id: str = ""
    file_id: str = ""
    spec_id: str = ""
    content_hash: str = ""
    manual_note: bool = False
    attrs: dict[str, Any] = field(default_factory=dict)

    def validate_for_write(self) -> None:
        if self.manual_note:
            if self.source_kind and self.source_kind != "manual":
                raise MemoryValidationError("Manual notes must use source_kind='manual'")
            return
        if not self.source_kind.strip() or not self.source_ref.strip():
            raise MemoryValidationError(
                "Memory facts require source metadata unless explicitly marked as manual notes"
            )

    @classmethod
    def manual(cls) -> MemorySourceMetadata:
        return cls(source_kind="manual", source_ref="manual", manual_note=True)

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "source_kind": self.source_kind,
            "source_ref": self.source_ref,
            "path": self.path,
            "workspace_root": self.workspace_root,
            "session_id": self.session_id,
            "step_id": self.step_id,
            "repo_id": self.repo_id,
            "file_id": self.file_id,
            "spec_id": self.spec_id,
            "content_hash": self.content_hash,
            "manual_note": self.manual_note,
        }
        payload.update(self.attrs)
        return {key: value for key, value in payload.items() if value not in ("", None, False)}

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        workspace_root: Path,
        source_kind: str,
        content_hash: str,
        attrs: dict[str, Any] | None = None,
    ) -> MemorySourceMetadata:
        return cls(
            source_kind=source_kind,
            source_ref=str(path),
            path=str(path),
            workspace_root=str(workspace_root),
            content_hash=content_hash,
            attrs=attrs or {},
        )


@dataclass(frozen=True)
class MemoryItem:
    content: str
    type: str
    scope: str
    source_metadata: MemorySourceMetadata
    id: str = ""
    confidence: float = 0.5
    sensitivity: str = "unknown"
    provider: str = "local_sqlite"
    repo_id: str = ""
    file_id: str = ""
    session_id: str = ""
    step_id: str = ""
    spec_id: str = ""
    expires_at: str = ""

    def validate_for_write(self) -> None:
        self.source_metadata.validate_for_write()
        if not self.content.strip():
            raise MemoryValidationError("Memory content cannot be empty")
        if not self.type.strip():
            raise MemoryValidationError("Memory type cannot be empty")
        if not self.scope.strip():
            raise MemoryValidationError("Memory scope cannot be empty")


@dataclass(frozen=True)
class MemorySearchResult:
    item: dict[str, Any]
    score: float = 0.0
    provider: str = "local_sqlite"


@dataclass(frozen=True)
class MemoryValidationResult:
    memory_id: str
    status: str
    stale_reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class MemoryProviderHealth:
    name: str
    available: bool
    status: str
    detail: str = ""


@dataclass(frozen=True)
class MemoryCandidate:
    id: str
    content: str
    type: str
    scope: str
    confidence: float
    source_metadata: dict[str, Any]
    evidence: dict[str, Any]
    status: str = "candidate"
