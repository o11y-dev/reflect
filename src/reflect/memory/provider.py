from __future__ import annotations

from typing import Protocol

from reflect.memory.models import (
    MemoryItem,
    MemoryProviderHealth,
    MemorySearchResult,
    MemoryValidationResult,
)


class MemoryProvider(Protocol):
    name: str

    def health(self) -> MemoryProviderHealth: ...

    def remember(self, item: MemoryItem) -> dict: ...

    def list(self, *, path: str = "", filters: dict | None = None, limit: int = 100) -> list[dict]: ...

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]: ...

    def inspect(self, memory_id: str) -> dict | None: ...

    def forget(self, memory_id: str) -> bool: ...

    def validate(self, memory_id: str) -> MemoryValidationResult: ...
