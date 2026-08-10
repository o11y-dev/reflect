from __future__ import annotations

import json
import re
from collections.abc import Mapping


def first_attr(attrs: Mapping[str, object], *keys: str) -> object | None:
    """Return the first present, non-empty telemetry attribute."""

    for key in keys:
        value = attrs.get(key)
        if value not in (None, ""):
            return value
    return None


def extract_skill_name_from_preview(preview: object) -> str:
    if not isinstance(preview, str) or not preview.strip():
        return ""
    try:
        payload = json.loads(preview)
    except json.JSONDecodeError:
        match = re.search(r'"skill"\s*:\s*"([^"]+)"', preview)
        return match.group(1).strip() if match else ""
    if isinstance(payload, dict):
        skill = payload.get("skill")
        if isinstance(skill, str):
            return skill.strip()
    return ""


def extract_skill_name_from_path(path: object) -> str:
    if not isinstance(path, str) or not path.strip():
        return ""
    match = re.search(r"(?:^|/)skills/(?:.*/)?([^/]+)/SKILL\.md$", path)
    return match.group(1).strip() if match else ""


def extract_skill_names_from_text(text: object) -> set[str]:
    if not isinstance(text, str) or not text.strip():
        return set()
    names: set[str] = set()
    for match in re.finditer(r"(?<![:\w.-])/([A-Za-z0-9][A-Za-z0-9_-]{1,60})", text):
        name = match.group(1).strip().strip(".,;:)")
        lowered = name.lower()
        if (
            "-" not in lowered
            and not lowered.endswith("skill")
            and lowered not in {"review", "investigate"}
        ):
            continue
        names.add(name)
    for match in re.finditer(r"`([^`/\n]{2,80})`\s+skill\b", text, flags=re.IGNORECASE):
        names.add(match.group(1).strip())
    return {name for name in names if name}


def clean_subagent_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    name = value.strip()
    if not name or "REDACTED" in name.upper() or name.startswith("["):
        return ""
    return name[:80]


def extract_subagent_name_from_tool(
    tool_name: object,
    attrs: Mapping[str, object] | None = None,
    preview: str = "",
) -> str:
    normalized_tool = str(tool_name or "").strip().lower()
    attrs = attrs or {}
    if not preview:
        preview = str(first_attr(attrs, "gen_ai.client.tool.input", "tool.input") or "")
    payload = _load_json_dict(preview)

    def first_value(*keys: str) -> str:
        for key in keys:
            value = first_attr(
                attrs,
                f"gen_ai.client.tool.input.{key}",
                f"tool.input.{key}",
            )
            if value in (None, ""):
                value = payload.get(key)
            cleaned = clean_subagent_name(value)
            if cleaned:
                return cleaned
        return ""

    if normalized_tool in {"subagent", "agent"}:
        return first_value("subagent_type", "agent_type", "name", "agent_id", "description")
    if normalized_tool in {"task", "read_agent"}:
        return first_value("agent_id", "name", "agent_type")
    return ""


def extract_subagent_names_from_text(text: object) -> set[str]:
    if not isinstance(text, str) or not text.strip():
        return set()
    names: set[str] = set()
    for match in re.finditer(r"`([^`/\n]{2,80})`\s+subagent\b", text, flags=re.IGNORECASE):
        names.add(match.group(1).strip())
    for match in re.finditer(
        r"\b(?:use|run|invoke|launch|call)\s+(?:the\s+)?([A-Za-z0-9][A-Za-z0-9_-]{2,80})\s+subagent\b",
        text,
        flags=re.IGNORECASE,
    ):
        names.add(match.group(1).strip())
    return {name for name in names if name}


def _load_json_dict(value: str) -> dict[str, object]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "clean_subagent_name",
    "extract_skill_name_from_path",
    "extract_skill_name_from_preview",
    "extract_skill_names_from_text",
    "extract_subagent_name_from_tool",
    "extract_subagent_names_from_text",
    "first_attr",
]
