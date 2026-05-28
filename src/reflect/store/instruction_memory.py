from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


_INSTRUCTION_NAMES = {
    "AGENTS.md": ("agent_instruction", "project"),
    "CLAUDE.md": ("claude_memory", "project"),
    "CLAUDE.local.md": ("claude_memory", "project_local"),
    "GEMINI.md": ("gemini_memory", "project"),
    ".cursorrules": ("cursor_legacy_rule", "project"),
    "copilot-instructions.md": ("copilot_instruction", "project"),
}

def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stable_id(path: Path) -> str:
    return f"instruction_{hashlib.sha1(str(path).encode('utf-8')).hexdigest()}"


def _preview(text: str, *, max_chars: int = 360) -> str:
    cleaned = " ".join(line.strip() for line in text.splitlines() if line.strip())
    return cleaned[:max_chars]


def _user_memory_files(home_root: Path) -> tuple[Path, ...]:
    return (
        home_root / ".claude" / "CLAUDE.md",
        home_root / ".gemini" / "GEMINI.md",
        home_root / ".cursor" / ".cursorrules",
    )


def _classify_instruction(path: Path, workspace_root: Path | None, *, home_root: Path) -> tuple[str, str]:
    name = path.name
    if path.as_posix().endswith(".github/instructions/" + name):
        return "copilot_instruction", "path"
    if ".cursor/rules/" in path.as_posix():
        return "cursor_rule", "path"
    if ".cursor/agents/" in path.as_posix():
        return "cursor_agent", "path"
    if ".cursor/plans/" in path.as_posix():
        return "cursor_plan", "user"
    if path in _user_memory_files(home_root):
        kind = _INSTRUCTION_NAMES.get(name, ("instruction", "user"))[0]
        if path.as_posix().endswith("/.cursor/.cursorrules"):
            kind = "cursor_legacy_rule"
        return kind, "user"
    if workspace_root is not None:
        try:
            path.relative_to(workspace_root)
            return _INSTRUCTION_NAMES.get(name, ("instruction", "project"))
        except ValueError:
            pass
    return _INSTRUCTION_NAMES.get(name, ("instruction", "project"))


def _candidate_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []

    candidates: set[Path] = set()
    for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", ".cursorrules"):
        candidate = root / name
        if candidate.is_file():
            candidates.add(candidate)

    copilot_instruction = root / ".github" / "copilot-instructions.md"
    if copilot_instruction.is_file():
        candidates.add(copilot_instruction)

    github_instructions = root / ".github" / "instructions"
    if github_instructions.is_dir():
        candidates.update(p for p in github_instructions.rglob("*.instructions.md") if p.is_file())

    copilot_instructions = root / ".github" / "copilot-instructions.md"
    if copilot_instructions.is_file():
        candidates.add(copilot_instructions)

    cursor_rules = root / ".cursor" / "rules"
    if cursor_rules.is_dir():
        candidates.update(p for p in cursor_rules.rglob("*.md") if p.is_file())
        candidates.update(p for p in cursor_rules.rglob("*.mdc") if p.is_file())

    cursor_agents = root / ".cursor" / "agents"
    if cursor_agents.is_dir():
        candidates.update(p for p in cursor_agents.rglob("*.md") if p.is_file())
        candidates.update(p for p in cursor_agents.rglob("*.mdc") if p.is_file())

    cursor_plans = root / ".cursor" / "plans"
    if cursor_plans.is_dir():
        candidates.update(p for p in cursor_plans.rglob("*.plan.md") if p.is_file())

    return sorted(candidates)


def _recursive_instruction_files(root: Path) -> list[Path]:
    if not root.exists() or not root.is_dir():
        return []
    candidates: set[Path] = set()
    for name in ("AGENTS.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", ".cursorrules"):
        candidates.update(p for p in root.rglob(name) if p.is_file())
    return sorted(candidates)


def discover_instruction_files(workspace_root: Path, *, home_root: Path | None = None) -> list[Path]:
    roots: list[Path] = []
    seen_roots: set[Path] = set()
    for candidate in [workspace_root, *workspace_root.parents]:
        resolved = candidate.resolve()
        if resolved in seen_roots:
            continue
        seen_roots.add(resolved)
        roots.append(candidate)
    if home_root is None:
        home_root = Path.home()
    if home_root.exists():
        resolved_home = home_root.resolve()
        if resolved_home not in seen_roots:
            roots.append(home_root)

    found: dict[str, Path] = {}
    for root in roots:
        for path in _candidate_files(root):
            found[str(path.resolve())] = path
        if root == workspace_root:
            for path in _recursive_instruction_files(root):
                found[str(path.resolve())] = path

    for path in _user_memory_files(home_root):
        if path.is_file():
            found[str(path.resolve())] = path

    return [found[key] for key in sorted(found)]


def upsert_instruction_memories(
    conn: sqlite3.Connection,
    *,
    workspace_root: Path,
    home_root: Path | None = None,
) -> dict[str, int]:
    files = discover_instruction_files(workspace_root, home_root=home_root)
    timestamp = _now()
    inserted = 0
    updated = 0

    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        kind, scope = _classify_instruction(path, workspace_root, home_root=home_root or Path.home())
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        preview = _preview(text)
        stat = path.stat()
        raw_attrs = {
            "path": str(path),
            "name": path.name,
            "kind": kind,
            "scope": scope,
            "workspace_root": str(workspace_root),
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
        }
        memory_id = _stable_id(path)
        existed = conn.execute("SELECT 1 FROM memories WHERE id = ?", (memory_id,)).fetchone() is not None
        conn.execute(
            """
            INSERT INTO memories(
              id, scope, type, content_hash, content_preview_redacted, confidence,
              sensitivity, source, last_seen_at, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              scope = excluded.scope,
              type = excluded.type,
              content_hash = excluded.content_hash,
              content_preview_redacted = excluded.content_preview_redacted,
              confidence = excluded.confidence,
              sensitivity = excluded.sensitivity,
              source = excluded.source,
              last_seen_at = excluded.last_seen_at,
              raw_attrs_json = excluded.raw_attrs_json,
              updated_at = excluded.updated_at
            """,
            (
                memory_id,
                scope,
                kind,
                content_hash,
                preview,
                1.0,
                "private" if scope == "user" else "unknown",
                "filesystem_instruction_scan",
                raw_attrs["mtime"],
                json.dumps(raw_attrs, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        if existed:
            updated += 1
        else:
            inserted += 1

    conn.commit()
    return {"discovered": len(files), "inserted": inserted, "updated": updated}
