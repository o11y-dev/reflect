from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.memory.models import MemoryProviderHealth, MemorySearchResult
from reflect.memory.omega_provider import OmegaMemoryProvider
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


class FakeOmegaStore:
    def __init__(self, *, db_path):
        self.db_path = db_path
        self.closed = False
        self.stored: list[dict] = []
        self.rows = [
            SimpleNamespace(
                id="mem-omega-1",
                content="Keep release gates evidence-backed.",
                metadata={
                    "event_type": "constraint",
                    "project": "/workspace/reflect",
                    "reflect_memory": {
                        "type": "repo_convention",
                        "scope": "project",
                        "source_metadata": {
                            "workspace_root": "/workspace/reflect",
                            "path": "/workspace/reflect/AGENTS.md",
                        },
                    },
                },
                created_at=datetime(2026, 7, 20, tzinfo=UTC),
                access_count=2,
                relevance=0.91,
                strength=0.8,
            )
        ]

    def node_count(self):
        return len(self.rows)

    def store(self, **kwargs):
        self.stored.append(kwargs)
        return "mem-omega-new"

    def query(self, *_args, **_kwargs):
        return self.rows

    def query_by_type(self, *_args, **_kwargs):
        return self.rows

    def get_recent(self, *, limit):
        return self.rows[:limit]

    def get_by_type(self, _event_type, *, limit):
        return self.rows[:limit]

    def get_by_session(self, _session_id, *, limit):
        return self.rows[:limit]

    def get_node(self, memory_id, *, track_access):
        assert track_access is False
        return self.rows[0] if memory_id == "mem-omega-1" else None

    def delete_node(self, memory_id):
        return memory_id == "mem-omega-1"

    def close(self):
        self.closed = True


def _provider(tmp_path):
    home = tmp_path / ".omega"
    home.mkdir()
    (home / "omega.db").touch()
    stores = []

    def factory(**kwargs):
        store = FakeOmegaStore(**kwargs)
        stores.append(store)
        return store

    return (
        OmegaMemoryProvider(
            home,
            store_factory=factory,
            module_available=lambda: True,
        ),
        stores,
    )


def test_omega_health_distinguishes_installation_and_setup(tmp_path):
    missing_package = OmegaMemoryProvider(tmp_path, module_available=lambda: False)
    missing_store = OmegaMemoryProvider(tmp_path, module_available=lambda: True)
    provider, stores = _provider(tmp_path)

    assert missing_package.health().status == "not_installed"
    assert missing_store.health().status == "not_configured"
    health = provider.health()

    assert health.available is True
    assert health.status == "ok"
    assert "1 memories" in health.detail
    assert stores[0].closed is True


def test_omega_remember_uses_public_store_contract_and_preserves_provenance(tmp_path):
    provider, stores = _provider(tmp_path)
    item = MemoryItem(
        id="reflect-memory-1",
        content="Keep release gates evidence-backed.",
        type="repo_convention",
        scope="project",
        confidence=0.9,
        sensitivity="private",
        source_metadata=MemorySourceMetadata(
            source_kind="filesystem_instruction_scan",
            source_ref="/workspace/reflect/AGENTS.md",
            path="/workspace/reflect/AGENTS.md",
            workspace_root="/workspace/reflect",
        ),
    )

    result = provider.remember(item)
    stored = stores[0].stored[0]

    assert result == {
        "id": "mem-omega-new",
        "memory_id": "mem-omega-new",
        "provider": "omega",
    }
    assert stored["metadata"]["event_type"] == "constraint"
    assert stored["metadata"]["reflect_memory"]["type"] == "repo_convention"
    assert stored["metadata"]["reflect_memory"]["source_metadata"]["path"].endswith(
        "AGENTS.md"
    )
    assert stored["source_uri"].endswith("AGENTS.md")
    assert stored["sensitivity"] == "private"
    assert stores[0].closed is True


def test_omega_search_returns_typed_provider_results_and_honors_path(tmp_path):
    provider, stores = _provider(tmp_path)

    results = provider.search(
        "release gate",
        path="/workspace/reflect",
        filters={"type": "repo_convention", "scope": "project"},
        limit=5,
    )

    assert len(results) == 1
    assert results[0].provider == "omega"
    assert results[0].score == 0.91
    assert results[0].item["id"] == "mem-omega-1"
    assert results[0].item["source_metadata"]["workspace_root"] == "/workspace/reflect"
    assert stores[0].closed is True


def test_omega_inspect_forget_and_validate(tmp_path):
    provider, _stores = _provider(tmp_path)

    assert provider.inspect("mem-omega-1")["type"] == "repo_convention"
    assert provider.inspect("missing") is None
    assert provider.forget("mem-omega-1") is True
    assert provider.validate("mem-omega-1").status == "validated"
    stale = provider.validate("missing")
    assert stale.status == "stale"
    assert stale.stale_reason == "provider_memory_missing"


class FakeOmegaProvider:
    name = "omega"

    def health(self):
        return MemoryProviderHealth("omega", True, "ok", "fake")

    def remember(self, _item):
        return {"id": "mem-omega-mirrored"}

    def search(self, _query, *, path="", filters=None, limit=20):
        return [
            MemorySearchResult(
                item={"id": "mem-omega-mirrored", "content": "Mirrored memory"},
                score=0.8,
                provider="omega",
            )
        ]


def test_generic_session_memory_routes_to_omega_and_keeps_local_row(tmp_path):
    conn = connect_sqlite(tmp_path / "reflect.db")
    migrate(conn)
    service = MemoryService(conn)
    try:
        with (
            patch.dict("os.environ", {"REFLECT_OMEGA_MEMORY_ENABLED": "true"}, clear=True),
            patch("reflect.memory.registry.OmegaMemoryProvider", return_value=FakeOmegaProvider()),
        ):
            row = service.remember(
                MemoryItem(
                    id="reflect-memory",
                    content="Mirror generic session memory.",
                    type="workflow_note",
                    scope="session",
                    source_metadata=MemorySourceMetadata.manual(),
                ),
                semantic_domain="generic_agent_session",
            )
            searched = service.search(
                "mirrored",
                path=None,
                provider="omega",
            )
    finally:
        conn.close()

    assert row["provider"] == "omega"
    assert row["provider_status"] == "mirrored"
    assert row["provider_memory_id"] == "mem-omega-mirrored"
    assert searched[0]["provider"] == "omega"
