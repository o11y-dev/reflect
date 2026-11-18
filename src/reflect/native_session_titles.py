"""Read saved chat labels for the local dashboard by full session ID."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

import yaml

from reflect.agent_capabilities import get_agent_capability
from reflect.opencode_store import OpenCodeSessionStore, opencode_database_path
from reflect.utils import _json_loads, _load_json_lines

_TITLE_LIMIT = 200
_METADATA_LIMIT = 64 * 1024
_CLAUDE_TAIL_LIMIT = 1024 * 1024
_GEMINI_RECORD_LIMIT = 8 * 1024 * 1024


def _title(value: object) -> str:
    return " ".join(value.split())[:_TITLE_LIMIT] if isinstance(value, str) else ""


def _cursor_title(directory: Path) -> str:
    try:
        with (directory / "meta.json").open("rb") as stream:
            data = stream.read(_METADATA_LIMIT + 1)
        if len(data) <= _METADATA_LIMIT:
            metadata = _json_loads(data)
            if isinstance(metadata, dict) and (title := _title(metadata.get("title"))):
                return title
    except (OSError, ValueError):
        pass

    # The SQLite store contains chat blobs and private keys. Read only the
    # bounded metadata record and return only its name.
    path = directory / "store.db"
    if not path.is_file():
        return ""
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        # A closed chat can be read without creating WAL sidecars.
        if not path.with_name("store.db-wal").exists():
            uri += "&immutable=1"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.05)) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = '0' AND length(value) <= ?",
                (_METADATA_LIMIT * 2,),
            ).fetchone()
        if row and isinstance(row[0], str):
            payload = row[0]
            metadata = _json_loads(payload if payload.lstrip().startswith("{") else bytes.fromhex(payload))
            if isinstance(metadata, dict):
                return _title(metadata.get("name"))
    except (OSError, ValueError, sqlite3.Error):
        pass
    return ""


def _cursor_titles(home: Path, ids: set[str]) -> dict[str, str]:
    titles = {}
    for directory in sorted((home / "chats").glob("*/*")):
        session_id = directory.name
        if session_id in ids and session_id not in titles and (title := _cursor_title(directory)):
            titles[session_id] = title
    return titles


def _codex_titles(home: Path, ids: set[str]) -> dict[str, str]:
    titles = {}
    try:
        for row in _load_json_lines(home / "session_index.jsonl"):
            session_id = row.get("id")
            if isinstance(session_id, str) and session_id in ids and (title := _title(row.get("thread_name"))):
                titles[session_id] = title
    except (OSError, ValueError):
        pass
    return titles


def _claude_title(path: Path) -> str:
    auto_title = ""
    try:
        with path.open("rb") as stream:
            end = stream.seek(0, 2)
            stream.seek(max(0, end - _CLAUDE_TAIL_LIMIT))
            tail = stream.read(_CLAUDE_TAIL_LIMIT)
    except OSError:
        return ""
    if b'"custom-title"' not in tail and b'"ai-title"' not in tail:
        return ""
    for line in reversed(tail.splitlines()):
        if b'"custom-title"' not in line and b'"ai-title"' not in line:
            continue
        try:
            row = _json_loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("sessionId") != path.stem:
            continue
        if row.get("type") == "custom-title" and (title := _title(row.get("customTitle"))):
            return title
        if row.get("type") == "ai-title" and not auto_title:
            auto_title = _title(row.get("aiTitle"))
    return auto_title


def _claude_titles(home: Path, ids: set[str]) -> dict[str, str]:
    titles = {}
    for path in sorted((home / "projects").glob("*/*.jsonl")):
        if path.stem in ids and path.stem not in titles and (title := _claude_title(path)):
            titles[path.stem] = title
    return titles


def _copilot_titles(home: Path, ids: set[str]) -> dict[str, str]:
    titles = {}
    for path in (home / "session-state").glob("*/workspace.yaml"):
        session_id = path.parent.name
        if session_id not in ids:
            continue
        try:
            with path.open("rb") as stream:
                contents = stream.read(_METADATA_LIMIT + 1)
            if len(contents) > _METADATA_LIMIT:
                continue
            metadata = yaml.safe_load(contents)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if not isinstance(metadata, dict) or metadata.get("id") != session_id:
            continue
        if title := _title(metadata.get("name")) or _title(metadata.get("summary")):
            titles[session_id] = title
    return titles


def _gemini_titles(home: Path, ids: set[str]) -> dict[str, str]:
    titles = {}
    for path in sorted((home / "tmp").glob("**/chats/session-*.json"), reverse=True):
        try:
            with path.open("rb") as stream:
                contents = stream.read(_GEMINI_RECORD_LIMIT + 1)
            if len(contents) > _GEMINI_RECORD_LIMIT:
                continue
            metadata = _json_loads(contents)
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict):
            continue
        session_id = metadata.get("sessionId")
        if not isinstance(session_id, str) or session_id not in ids or session_id in titles:
            continue
        if title := _title(metadata.get("summary")):
            titles[session_id] = title
            if len(titles) == len(ids):
                break
    return titles


def _opencode_titles(_home: Path, ids: set[str]) -> dict[str, str]:
    try:
        titles = OpenCodeSessionStore(opencode_database_path()).titles(ids)
    except (OSError, sqlite3.Error):
        return {}
    return {session_id: title for session_id, value in titles.items() if (title := _title(value))}


_READERS = {
    "claude": _claude_titles,
    "codex": _codex_titles,
    "copilot": _copilot_titles,
    "cursor": _cursor_titles,
    "gemini": _gemini_titles,
    "opencode": _opencode_titles,
}


def read_native_session_titles(ids_by_agent: dict[str, set[str]]) -> dict[str, dict[str, str]]:
    return {
        agent: reader(capability.home(), ids_by_agent[agent])
        for agent, reader in _READERS.items()
        if ids_by_agent.get(agent) and (capability := get_agent_capability(agent)) is not None
    }


def with_native_session_titles(payload: dict) -> dict:
    """Enrich only the visible page, without mutating the response cache."""
    sessions = payload.get("sessions")
    if not isinstance(sessions, list):
        return payload
    ids_by_agent: dict[str, set[str]] = defaultdict(set)
    for session in sessions:
        agent = session.get("agent")
        session_id = session.get("id")
        if agent in _READERS and isinstance(session_id, str):
            ids_by_agent[agent].add(session_id)
    titles = read_native_session_titles(ids_by_agent)
    return {
        **payload,
        "sessions": [
            {**session, "native_title": title}
            if (title := titles.get(session.get("agent"), {}).get(session.get("id"))) else session
            for session in sessions
        ],
    }
