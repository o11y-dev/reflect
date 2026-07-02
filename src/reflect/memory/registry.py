from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from reflect.memory.models import (
    MemoryItem,
    MemoryProviderHealth,
    MemorySearchResult,
    MemoryValidationResult,
)
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


class LiteLLMMemoryProvider:
    name = "litellm"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_key_env: str = "LITELLM_API_KEY",
        key_prefix: str = "reflect:",
        timeout: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.api_key_env = api_key_env
        self.key_prefix = key_prefix or "reflect:"
        self.timeout = timeout

    def health(self) -> MemoryProviderHealth:
        if not self.base_url:
            return MemoryProviderHealth(self.name, False, "not_configured", "LITELLM_MEMORY_URL is not set")
        if not self.api_key:
            return MemoryProviderHealth(self.name, False, "not_configured", f"{self.api_key_env} is not set")
        try:
            self._json_request("GET", "/v1/memory", query={"page_size": "1"})
        except urllib_error.HTTPError as exc:
            status = "auth_error" if exc.code in (401, 403) else "error"
            return MemoryProviderHealth(self.name, False, status, f"HTTP {exc.code}")
        except (OSError, urllib_error.URLError) as exc:
            return MemoryProviderHealth(self.name, False, "unreachable", str(exc))
        return MemoryProviderHealth(self.name, True, "ok", self.base_url)

    def remember(self, item: MemoryItem) -> dict:
        key = self._key_for_item(item)
        payload: dict[str, Any] = {
            "value": item.content,
            "metadata": self._metadata_for_item(item),
        }
        user_id = os.environ.get("LITELLM_MEMORY_USER_ID", "").strip()
        team_id = os.environ.get("LITELLM_MEMORY_TEAM_ID", "").strip()
        if user_id:
            payload["user_id"] = user_id
        if team_id:
            payload["team_id"] = team_id
        response = self._json_request("PUT", f"/v1/memory/{urllib_parse.quote(key, safe='')}", payload)
        if isinstance(response, dict):
            response.setdefault("id", response.get("memory_id") or response.get("key") or key)
            return response
        return {"id": key, "provider_response": response}

    def list(self, *, path: str = "", filters: dict | None = None, limit: int = 100) -> list[dict]:
        filters = filters or {}
        query = {
            "page_size": str(max(1, min(int(limit or 100), 500))),
            "key_prefix": str(filters.get("key_prefix") or self.key_prefix),
        }
        if filters.get("key"):
            query = {"key": str(filters["key"]), "page_size": query["page_size"]}
        response = self._json_request("GET", "/v1/memory", query=query)
        memories = response.get("memories") if isinstance(response, dict) else response
        if not isinstance(memories, list):
            return []
        return [self._normalize_memory(row) for row in memories if isinstance(row, dict)]

    def search(
        self,
        query: str,
        *,
        path: str = "",
        filters: dict | None = None,
        limit: int = 20,
    ) -> list[MemorySearchResult]:
        lowered = query.lower()
        rows = self.list(path=path, filters=filters, limit=max(limit, 50))
        matches = [
            row
            for row in rows
            if lowered in str(row.get("key") or "").lower()
            or lowered in str(row.get("content_preview_redacted") or row.get("content") or "").lower()
            or lowered in json.dumps(row.get("source_metadata") or {}, sort_keys=True).lower()
        ][:limit]
        return [MemorySearchResult(item=row, provider=self.name) for row in matches]

    def inspect(self, memory_id: str) -> dict | None:
        try:
            response = self._json_request("GET", f"/v1/memory/{urllib_parse.quote(memory_id, safe='')}")
        except urllib_error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        return self._normalize_memory(response) if isinstance(response, dict) else None

    def forget(self, memory_id: str) -> bool:
        response = self._json_request("DELETE", f"/v1/memory/{urllib_parse.quote(memory_id, safe='')}")
        return bool(isinstance(response, dict) and response.get("deleted", True))

    def validate(self, memory_id: str) -> MemoryValidationResult:
        try:
            found = self.inspect(memory_id)
        except Exception as exc:  # noqa: BLE001 - validation reports provider reachability
            return MemoryValidationResult(memory_id, "error", error=str(exc))
        if found is None:
            return MemoryValidationResult(memory_id, "stale", stale_reason="provider_memory_missing")
        return MemoryValidationResult(memory_id, "validated")

    def _json_request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        query: dict[str, str] | None = None,
    ):
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urllib_parse.urlencode(query)}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib_request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        with urllib_request.urlopen(req, timeout=self.timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def _key_for_item(self, item: MemoryItem) -> str:
        raw_key = item.id or item.source_metadata.content_hash or item.content[:80]
        sanitized = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in raw_key).strip("_")
        return f"{self.key_prefix}{item.scope}:{item.type}:{sanitized or 'memory'}"

    def _metadata_for_item(self, item: MemoryItem) -> dict[str, Any]:
        return {
            "reflect_memory_id": item.id,
            "type": item.type,
            "scope": item.scope,
            "confidence": item.confidence,
            "sensitivity": item.sensitivity,
            "source_metadata": item.source_metadata.to_json_dict(),
        }

    def _normalize_memory(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        source_metadata = metadata.get("source_metadata") if isinstance(metadata.get("source_metadata"), dict) else metadata
        return {
            "id": row.get("key") or row.get("memory_id") or "",
            "memory_id": row.get("memory_id") or "",
            "provider_memory_id": row.get("key") or row.get("memory_id") or "",
            "key": row.get("key") or "",
            "content": row.get("value") or "",
            "content_preview_redacted": str(row.get("value") or "")[:1000],
            "type": metadata.get("type") or "",
            "scope": metadata.get("scope") or "",
            "provider": self.name,
            "source_metadata": source_metadata,
            "raw_attrs": row,
        }


class MemoryPalaceProvider(AgentMemoryProvider):
    name = "memorypalace"

    def __init__(self, base_url: str, api_key: str = ""):
        super().__init__(base_url)
        self._detail = "MEMORYPALACE_URL is not configured"
        self.api_key = api_key

    def health(self) -> MemoryProviderHealth:
        if not self.base_url:
            return MemoryProviderHealth(self.name, False, "not_configured", "MEMORYPALACE_URL is not set")
        return super().health()

    def _json_request(self, path: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib_request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=5) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}


@dataclass
class MemoryProviderRegistry:
    local_sqlite: LocalSQLiteMemoryProvider

    def providers(self) -> dict[str, object]:
        litellm_api_key_env = os.environ.get("REFLECT_LITELLM_MEMORY_API_KEY_ENV", "LITELLM_API_KEY")
        return {
            "local_sqlite": self.local_sqlite,
            "agentmemory": AgentMemoryProvider(os.environ.get("AGENTMEMORY_URL", "")),
            "litellm": LiteLLMMemoryProvider(
                os.environ.get("LITELLM_MEMORY_URL", os.environ.get("REFLECT_LITELLM_MEMORY_URL", "")),
                os.environ.get(litellm_api_key_env, ""),
                api_key_env=litellm_api_key_env,
                key_prefix=os.environ.get("LITELLM_MEMORY_KEY_PREFIX", "reflect:"),
            ),
            "memorypalace": MemoryPalaceProvider(
                os.environ.get("MEMORYPALACE_URL", ""),
                os.environ.get("MEMORYPALACE_API_KEY", ""),
            ),
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
