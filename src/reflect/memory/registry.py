from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib import error as urllib_error
from urllib import request as urllib_request

from reflect.memory.models import MemoryItem, MemoryProviderHealth, MemorySearchResult
from reflect.memory.sqlite_provider import LocalSQLiteMemoryProvider


class StubMemoryProvider:
    def __init__(self, name: str, detail: str):
        self.name = name
        self._detail = detail

    def health(self) -> MemoryProviderHealth:
        return MemoryProviderHealth(self.name, False, "not_configured", self._detail)

    def remember(self, item: MemoryItem) -> dict:
        raise RuntimeError(f"{self.name} provider is not configured")

    def list(self, *, path: str = "", filters: dict | None = None, limit: int = 100) -> list[dict]:
        return []

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        return []

    def inspect(self, memory_id: str) -> dict | None:
        return None

    def forget(self, memory_id: str) -> bool:
        return False

    def validate(self, memory_id: str):
        raise RuntimeError(f"{self.name} provider is not configured")


class AgentMemoryProvider(StubMemoryProvider):
    name = "agentmemory"

    def __init__(self, base_url: str):
        super().__init__(self.name, "AGENTMEMORY_URL is not configured")
        self.base_url = base_url.rstrip("/")

    def health(self) -> MemoryProviderHealth:
        if not self.base_url:
            return MemoryProviderHealth(self.name, False, "not_configured", "AGENTMEMORY_URL is not set")
        for suffix in ("/health", ""):
            try:
                with urllib_request.urlopen(f"{self.base_url}{suffix}", timeout=2) as response:
                    if 200 <= response.status < 500:
                        return MemoryProviderHealth(self.name, True, "ok", self.base_url)
            except (OSError, urllib_error.URLError):
                continue
        return MemoryProviderHealth(self.name, False, "unreachable", self.base_url)

    def remember(self, item: MemoryItem) -> dict:
        payload = {
            "id": item.id,
            "content": item.content,
            "type": item.type,
            "scope": item.scope,
            "metadata": item.source_metadata.to_json_dict(),
        }
        response = self._json_request("/memories", payload)
        return response if isinstance(response, dict) else {"provider_response": response}

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        payload = {"query": query, "limit": limit, "filters": filters or {}}
        if path:
            payload["path"] = path
        response = self._json_request("/search", payload)
        if isinstance(response, dict):
            items = response.get("results") or response.get("memories") or []
        else:
            items = response if isinstance(response, list) else []
        return [
            MemorySearchResult(
                item=item if isinstance(item, dict) else {"content": str(item)},
                provider=self.name,
            )
            for item in items
        ]

    def _json_request(self, path: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib_request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=5) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


@dataclass
class MemoryProviderRegistry:
    local_sqlite: LocalSQLiteMemoryProvider

    def providers(self) -> dict[str, object]:
        return {
            "local_sqlite": self.local_sqlite,
            "agentmemory": AgentMemoryProvider(os.environ.get("AGENTMEMORY_URL", "")),
            "mem0": StubMemoryProvider("mem0", "Mem0 adapter is discovery/health-only in this release"),
            "graphiti": StubMemoryProvider("graphiti", "Graphiti adapter is discovery/health-only in this release"),
            "tencentdb_agent_memory": StubMemoryProvider(
                "tencentdb_agent_memory",
                "TencentDB-Agent-Memory adapter is discovery/health-only in this release",
            ),
        }

    def get(self, name: str):
        providers = self.providers()
        if name not in providers:
            raise KeyError(f"Unknown memory provider: {name}")
        return providers[name]

    def health(self) -> list[MemoryProviderHealth]:
        return [provider.health() for provider in self.providers().values()]
