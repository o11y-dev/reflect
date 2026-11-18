import json
import sqlite3
from contextlib import closing

from reflect.conversation_adapters import DEFAULT_CONVERSATION_ADAPTERS
from reflect.native_session_titles import with_native_session_titles


def test_native_titles_match_agent_and_full_session_id(tmp_path, monkeypatch):
    for agent in ("cursor", "codex", "claude"):
        monkeypatch.setenv(f"{agent.upper()}_HOME", str(tmp_path / agent))

    chats = tmp_path / "cursor" / "chats" / "workspace"
    main_chat = chats / "cursor-chat"
    main_chat.mkdir(parents=True)
    (main_chat / "meta.json").write_text(json.dumps({"title": "Work on title imports"}))
    child_chat = chats / "cursor-child"
    child_chat.mkdir()
    with closing(sqlite3.connect(child_chat / "store.db")) as conn, conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)")
        metadata = json.dumps({"name": "Check the adapter", "blobEncryptionKey": "private"})
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (metadata.encode().hex(),))

    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "session_index.jsonl").write_text(
        '{"id":"cursor-chat","thread_name":"Codex title with the same ID"}\n'
        '{"id":"codex-chat","thread_name":"Earlier title"}\n'
        '{"id":"codex-chat","thread_name":"Updated title"}\n'
    )

    claude_project = tmp_path / "claude" / "projects" / "project"
    claude_project.mkdir(parents=True)
    (claude_project / "claude-chat.jsonl").write_text(
        '{"type":"custom-title","sessionId":"claude-chat","customTitle":"Named Claude session"}\n'
        '{"type":"ai-title","sessionId":"claude-chat","aiTitle":"Generated title"}\n'
        '{"type":"custom-title","sessionId":"other-chat","customTitle":"Unrelated title"}\n'
    )
    payload = {"sessions": [
        {"agent": "cursor", "id": "cursor-chat"},
        {"agent": "cursor", "id": "cursor-child"},
        {"agent": "cursor", "id": "chat"},
        {"agent": "codex", "id": "codex-chat"},
        {"agent": "claude", "id": "claude-chat"},
        {"agent": "claude", "id": "other-chat"},
    ]}

    rows = with_native_session_titles(payload)["sessions"]
    assert [row.get("native_title") for row in rows] == [
        "Work on title imports", "Check the adapter", None,
        "Updated title", "Named Claude session", None,
    ]
    assert all("native_title" not in row for row in payload["sessions"])
    assert "private" not in json.dumps(rows)


def test_remaining_native_adapters_read_saved_labels(tmp_path, monkeypatch):
    for agent in ("copilot", "gemini", "opencode"):
        monkeypatch.setenv(f"{agent.upper()}_HOME", str(tmp_path / agent))

    copilot = tmp_path / "copilot" / "session-state"
    for session_id, metadata in {
        "copilot-chat": 'id: copilot-chat\nname: "Inspect: saved titles"\nsummary: Fallback title\n',
        "copilot-summary": "id: copilot-summary\nsummary: >\n  Review the\n  adapter\n",
        "copilot-mismatch": "id: other\nname: Other chat\n",
    }.items():
        workspace = copilot / session_id
        workspace.mkdir(parents=True)
        (workspace / "workspace.yaml").write_text(metadata, encoding="utf-8")

    gemini = tmp_path / "gemini" / "tmp" / "project" / "chats"
    gemini.mkdir(parents=True)
    (gemini / "session-timestamp-gemini.json").write_text(
        json.dumps({"sessionId": "gemini-chat", "summary": "Inspect API routes", "messages": []}),
        encoding="utf-8",
    )

    opencode = tmp_path / "opencode"
    opencode.mkdir()
    with closing(sqlite3.connect(opencode / "opencode.db")) as conn, conn:
        conn.execute("CREATE TABLE session(id TEXT PRIMARY KEY, title TEXT)")
        conn.execute("INSERT INTO session VALUES (?, ?)", ("opencode-chat", "Review configuration"))

    payload = {"sessions": [
        {"agent": "copilot", "id": "copilot-chat"},
        {"agent": "copilot", "id": "copilot-summary"},
        {"agent": "copilot", "id": "copilot-mismatch"},
        {"agent": "gemini", "id": "gemini-chat"},
        {"agent": "gemini", "id": "gemini"},
        {"agent": "opencode", "id": "opencode-chat"},
        {"agent": "opencode", "id": "missing"},
    ]}

    rows = with_native_session_titles(payload)["sessions"]
    assert [row.get("native_title") for row in rows] == [
        "Inspect: saved titles", "Review the adapter", None,
        "Inspect API routes", None, "Review configuration", None,
    ]


def test_native_title_readers_cover_every_conversation_adapter():
    from reflect.native_session_titles import _READERS

    assert set(_READERS) == set(DEFAULT_CONVERSATION_ADAPTERS.supported_agents())
