import importlib
import json

import pytest

from reflect.memory import MemoryItem, MemoryService, MemorySourceMetadata
from reflect.store.migrate import migrate
from reflect.store.sqlite import connect_sqlite


def workspace(conn, root, name, repo_id=None):
    root.mkdir(parents=True, exist_ok=True)
    conn.execute("INSERT OR IGNORE INTO agents(id, name, created_at, updated_at) VALUES ('agent', 'test', 'now', 'now')")
    if repo_id:
        conn.execute("INSERT OR IGNORE INTO repos(id, full_name, created_at, updated_at) VALUES (?, ?, 'now', 'now')", (repo_id, repo_id))
    conn.execute("INSERT INTO workspaces(id, root_path, path_hash, repo_id, label, source_key, created_at, updated_at) VALUES (?, ?, ?, ?, 'test', 'test', 'now', 'now')", (name, str(root), name, repo_id))


def pattern(conn, ws, label, count=2, *, kind="touched_folder", node_id=None):
    repo_id = conn.execute("SELECT repo_id FROM workspaces WHERE id=?", (ws,)).fetchone()[0]
    target = node_id or f"{kind}:{ws}:{label}"
    node_kind = {"touched_folder": "Folder", "used_skill": "Skill", "addressed_spec": "Spec", "used_tool": "Tool"}[kind]
    conn.execute("INSERT OR IGNORE INTO graph_nodes(id, kind, label, identity_key, first_seen_at, last_seen_at, created_at, updated_at) VALUES (?, ?, ?, ?, 'now', 'now', 'now', 'now')", (target, node_kind, label, target))
    for i in range(count):
        session = f"{ws}-{i}"
        conn.execute("INSERT OR IGNORE INTO sessions(id, agent_id, workspace_id, repo_id, started_at, status, created_at, updated_at) VALUES (?, 'agent', ?, ?, 'now', 'completed', 'now', 'now')", (session, ws, repo_id))
        conn.execute("INSERT OR IGNORE INTO graph_nodes(id, kind, label, first_seen_at, last_seen_at, created_at, updated_at) VALUES (?, 'Session', ?, 'now', 'now', 'now', 'now')", (session, session))
        conn.execute("INSERT OR IGNORE INTO graph_edges(id, source_node_id, target_node_id, kind, session_id, first_seen_at, last_seen_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'now', 'now', 'now', 'now')", (f"{session}:{target}", session, target, kind, session))
    conn.commit()


@pytest.fixture
def store(tmp_path):
    conn = connect_sqlite(tmp_path / "memory.db")
    migrate(conn)
    workspace(conn, tmp_path / "a", "a", "repo-a")
    workspace(conn, tmp_path / "b", "b", "repo-b")
    try:
        yield conn, MemoryService(conn)
    finally:
        conn.close()


def test_scope_precedes_threshold_ranking_and_limit(store, tmp_path):
    conn, service = store
    pattern(conn, "b", "foreign", 20)
    pattern(conn, "a", "module", 2)
    pattern(conn, "a", "shared", 1)
    pattern(conn, "b", "shared", 2)
    # Seed the persisted B list first: listing must be isolated too.
    assert service.candidates(path=tmp_path / "b")
    rows = service.candidates(path=tmp_path / "a", limit=1)
    assert len(rows) == 1
    assert rows[0]["evidence"]["target_label"] == "module"
    assert rows[0]["evidence"]["session_support"] == 2
    assert rows[0]["evidence"]["session_ids"] == ["a-0", "a-1"]
    assert rows[0]["scope_kind"] == "repo" and rows[0]["scope_id"] == "repo-a"
    assert len(service.candidates(path=tmp_path / "a")) == 1


def test_unknown_path_and_foreign_session_return_nothing(store, tmp_path):
    conn, service = store
    pattern(conn, "a", "src")
    pattern(conn, "b", "src")
    assert service.candidates(path=tmp_path / "a")
    before = conn.total_changes
    assert service.candidates(path=tmp_path / "unknown") == []
    assert service.candidates(path=tmp_path / "a", session_id="b-0") == []
    assert service.candidates(path=tmp_path / "a", session_id="missing") == []
    assert conn.total_changes == before
    rows = service.candidates(path=tmp_path / "a", session_id="a-0")
    assert rows[0]["evidence"]["session_ids"] == ["a-0", "a-1"]
    assert rows[0]["evidence"]["session_support"] == 2
    assert service.candidates(path=tmp_path / "a")[0]["evidence"]["session_support"] == 2


@pytest.mark.parametrize("fts", [True, False])
def test_identity_and_promoted_search_follow_repository_across_checkouts(store, tmp_path, fts):
    conn, _ = store
    service = MemoryService(conn, maintain_search_index=fts)
    workspace(conn, tmp_path / "checkout", "checkout", "repo-a")
    pattern(conn, "a", "src/memory")
    pattern(conn, "b", "src/memory")
    a = service.candidates(path=tmp_path / "a")[0]
    b = service.candidates(path=tmp_path / "b")[0]
    assert a["id"] != b["id"]
    pattern(conn, "checkout", "src/memory")
    updated = service.candidates(path=tmp_path / "checkout" / "src")[0]
    assert updated["id"] == a["id"]
    assert updated["evidence"]["session_support"] == 4
    assert "4 session(s)" in updated["content"]
    promoted = service.promote_candidate(a["id"])
    assert promoted["repo_id"] == "repo-a"
    assert promoted["source_metadata"]["scope_id"] == "repo-a"
    assert service.promote_candidate(a["id"])["id"] == promoted["id"]
    for root in ("a", "checkout"):
        assert [m["id"] for m in service.search("memory", path=tmp_path / root / "src")] == [promoted["id"]]
        assert [m["id"] for m in service.list_memories(path=tmp_path / root)] == [promoted["id"]]
    assert service.search("memory", path=tmp_path / "b") == []
    assert service.search("memory", path=tmp_path / "unknown") == []


def test_workspace_fallback_and_nested_repository_isolation(store, tmp_path):
    conn, service = store
    workspace(conn, tmp_path / "standalone", "standalone")
    workspace(conn, tmp_path / "a" / "nested", "nested", "repo-nested")
    pattern(conn, "standalone", "module")
    pattern(conn, "nested", "module")
    candidate = service.candidates(path=tmp_path / "standalone")[0]
    assert (candidate["scope_kind"], candidate["scope_id"]) == ("workspace", "standalone")
    memory = service.promote_candidate(candidate["id"])
    assert service.search("module", path=tmp_path / "standalone" / "src")[0]["id"] == memory["id"]
    assert service.candidates(path=tmp_path / "a") == []
    assert service.candidates(path=tmp_path / "a" / "nested")[0]["scope_id"] == "repo-nested"


def test_proposals_keep_useful_targets_without_claiming_success(store, tmp_path):
    conn, service = store
    pattern(conn, "a", "Read", 10, kind="used_tool")
    pattern(conn, "a", "/private/unrelated/folder", 10)
    pattern(conn, "a", ".", 10)
    pattern(conn, "a", "verify-tests", kind="used_skill")
    pattern(conn, "a", "Release", kind="addressed_spec", node_id="spec-one")
    pattern(conn, "a", "Release", kind="addressed_spec", node_id="spec-two")
    rows = service.candidates(path=tmp_path / "a")
    assert len(rows) == 3  # Separate contracts with the same title retain their identity.
    assert len({row["id"] for row in rows}) == 3
    assert any(row["content"].startswith("Skill observed: verify-tests") for row in rows)
    assert all("success" not in row["content"] for row in rows)


def test_migration_quarantines_legacy_memories_and_regenerates_candidates(tmp_path, monkeypatch):
    migrations = importlib.import_module("reflect.store.migrate")
    prior = [m for m in migrations.load_migrations() if m.version < 37]
    conn = connect_sqlite(tmp_path / "legacy.db")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(migrations, "load_migrations", lambda: prior)
            migrate(conn)
        service = MemoryService(conn)
        old = service.remember(MemoryItem(content="Legacy module memory", type="graph_pattern", scope="project",
            source_metadata=MemorySourceMetadata(source_kind="graph_candidate", source_ref="candidate-old", workspace_root=str(tmp_path / "a"))))
        conn.execute("UPDATE memories SET validation_status='validated' WHERE id=?", (old["id"],))
        conn.execute("INSERT INTO memory_candidates(id, scope, type, content_preview_redacted, status, promoted_memory_id, created_at, updated_at) VALUES ('candidate-old', 'project', 'graph_pattern', 'old', 'promoted', ?, 'now', 'now')", (old["id"],))
        conn.commit()
        assert migrate(conn) == [37]
        assert migrate(conn) == []
        assert service.inspect(old["id"])["validation_status"] == "needs_review"
        assert service.validate(old["id"])["status"] == "needs_review"
        assert service.search("Legacy", path=None) == []
        assert MemoryService(conn, maintain_search_index=False).search("Legacy", path=None) == []
        assert service.list_memories(path=None, all_memories=True)[0]["id"] == old["id"]
        with pytest.raises(ValueError, match="scope is unverified"):
            service.promote_candidate("candidate-old")
        workspace(conn, tmp_path / "a", "a", "repo-a")
        pattern(conn, "a", "module")
        regenerated = service.candidates(path=tmp_path / "a")
        assert len(regenerated) == 1 and regenerated[0]["id"] != "candidate-old"
        assert service.promote_candidate(regenerated[0]["id"])["repo_id"] == "repo-a"
        # The old provenance remains available for review; it was not guessed or rewritten.
        assert service.inspect(old["id"])["source_metadata"]["workspace_root"] == str(tmp_path / "a")
        assert json.loads(conn.execute("SELECT evidence_json FROM memory_candidates WHERE id='candidate-old'").fetchone()[0]) == {}
    finally:
        conn.close()
