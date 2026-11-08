from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from reflect.tool_outcomes import tool_failure

TURN_TERMINAL_KEYS = (
    "reflect.turn.terminal", "gen_ai.client.hook.event", "event.name",
    "reflect.telemetry.origin", "reflect.time.provenance", "reflect.turn.identity_provenance",
)
TOOL_NAME_KEYS = ("gen_ai.client.tool_name", "gen_ai.tool.name", "ide.tool_name", "tool.name", "tool_name")


def event_time(value: object) -> datetime | None:
    """Normalize event time without dropping sub-millisecond ordering."""
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, TypeError):
        return None


def observed_turn_terminal(attrs: Mapping[str, object]) -> bool:
    """A recorded harness stop closes observation; it does not prove success.

    Legacy hooks emitted Stop without the newer terminal flag. Accept their
    structured event and origin, never tool output, summaries, or MCP reports.
    Explicit non-terminal and synthetic records cannot establish a boundary.
    """
    if attrs.get("reflect.time.provenance") in {"synthetic", "inferred", "unavailable"}:
        return False
    if attrs.get("reflect.turn.identity_provenance") in {"synthetic", "inferred"}:
        return False
    terminal = attrs.get("reflect.turn.terminal")
    if isinstance(terminal, bool):
        return terminal
    event = str(first_attr(attrs, "gen_ai.client.hook.event", "event.name") or "").lower().rsplit(".", 1)[-1]
    origin = str(attrs.get("reflect.telemetry.origin") or "")
    if event == "stop":
        return origin.startswith("hook_")
    return origin.startswith("native_") and event in {
        "task_complete", "task_completed", "turn_complete", "turn_completed", "turncompleted",
    }


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


def recorded_tool_name(attrs: Mapping[str, object]) -> str:
    """Read explicit tool identity without turning event names into tools."""
    for key in TOOL_NAME_KEYS:
        value = attrs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def recorded_tool_call_id(attrs: Mapping[str, object]) -> str:
    return str(first_attr(attrs, "gen_ai.client.tool_use_id", "gen_ai.tool.call.id",
                          "tool.call_id", "tool_call_id", "tool_call.id", "tool.id") or "")


def is_subagent_launch_tool(tool_name: object) -> bool:
    name = str(tool_name or "").strip().lower()
    for prefix in ("functions.collaboration.", "collaboration.", "functions."):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    return name in {"subagent", "agent", "task", "spawn_agent", "collaborationspawn_agent", "delegate"}


@dataclass(frozen=True)
class SubagentFact:
    event_name: str
    role: str = ""
    name: str = ""
    agent_id: str = ""
    call_id: str = ""
    failed: bool = False

    @property
    def label(self) -> str:
        return self.role or self.name or "Role not recorded"


class SubagentTracker:
    """Reconcile streaming facts using recorded child and call IDs only."""

    def __init__(self) -> None:
        self.facts: dict[tuple[str, str, str], SubagentFact] = {}
        self.children: dict[tuple[str, str], str] = {}

    def result_fact(self, session: str, attrs: Mapping[str, object], event: str) -> SubagentFact | None:
        """Recover omitted tool identity only for an already recorded launch."""
        call_id = recorded_tool_call_id(attrs)
        if not call_id or event.lower() not in {"posttooluse", "posttoolusefailure", "aftermcpexecution"}:
            return None
        child = self.children.get((session, call_id), "")
        identity = f"agent:{child}" if child else f"call:{call_id}"
        if (session, "SubagentStart", identity) not in self.facts:
            return None
        return subagent_fact(attrs, event, tool_name="subagent")

    def observe(self, session: str, fact: SubagentFact, fallback: str) -> dict[str, Counter[str]]:
        delta: dict[str, Counter[str]] = {"SubagentStart": Counter(), "SubagentStop": Counter()}
        child = fact.agent_id or self.children.get((session, fact.call_id), "")
        identity = f"agent:{child}" if child else f"call:{fact.call_id}" if fact.call_id else fallback
        key = (session, fact.event_name, identity)
        prior = self.facts.pop(key, None)
        if prior:
            delta[fact.event_name][prior.label] -= 1
        if fact.failed and not child:
            return delta
        if child and fact.call_id:
            self.children[(session, fact.call_id)] = child
            call = self.facts.pop((session, fact.event_name, f"call:{fact.call_id}"), None)
            if call:
                delta[fact.event_name][call.label] -= 1
                prior = prior or call
        counterpart = self.facts.get((session, "SubagentStart" if fact.event_name == "SubagentStop"
                                      else "SubagentStop", identity))
        role = fact.role or (prior.role if prior else "") or (counterpart.role if counterpart else "")
        name = fact.name or (prior.name if prior else "") or (counterpart.name if counterpart else "")
        current = replace(fact, role=role, name=name, agent_id=child)
        self.facts[key] = current
        delta[fact.event_name][current.label] += 1
        if counterpart and role and not counterpart.role:
            delta[counterpart.event_name][counterpart.label] -= 1
            updated = replace(counterpart, role=role)
            self.facts[(session, counterpart.event_name, identity)] = updated
            delta[counterpart.event_name][updated.label] += 1
        return delta


def subagent_fact(
    attrs: Mapping[str, object], fallback_event: str = "", *, tool_name: object = "", preview: str = "",
) -> SubagentFact | None:
    """Extract explicit lifecycle or launch-tool evidence, never prose mentions.

    Roles, task names and invocation IDs remain distinct. A known launch with
    no captured role stays observable without inventing a role from its ID.
    """
    event = str(first_attr(attrs, "gen_ai.client.hook.event", "ide.hook.event", "event.name")
                or fallback_event).rsplit(".", 1)[-1].replace("_", "").lower()
    lifecycle = {"subagentstart": "SubagentStart", "subagentstop": "SubagentStop"}.get(event)
    launch = is_subagent_launch_tool(tool_name or recorded_tool_name(attrs))
    if not lifecycle and not launch:
        return None
    payload = _load_json_dict(preview or first_attr(attrs, "gen_ai.client.tool.input", "tool.input") or "")
    output = _load_json_dict(first_attr(attrs, "gen_ai.client.tool.output", "tool.output") or "")

    def value(attribute_keys: tuple[str, ...], input_keys: tuple[str, ...] = ()) -> str:
        for key in attribute_keys:
            cleaned = clean_subagent_name(attrs.get(key))
            if cleaned and cleaned.lower() != "unknown":
                return cleaned
        if launch:
            for key in input_keys:
                cleaned = clean_subagent_name(first_attr(attrs, f"gen_ai.client.tool.input.{key}",
                                                         f"tool.input.{key}") or payload.get(key))
                if cleaned and cleaned.lower() != "unknown":
                    return cleaned
        return ""

    role = value(("gen_ai.client.subagent_type", "ide.subagent_type", "subagent.type",
                  "subagent_type", "agent_type") + (("gen_ai.agent.type", "agent.type") if lifecycle else ()),
                 ("subagent_type", "agent_type"))
    name = value(("subagent.name",) + (("gen_ai.agent.name", "agent.name", "agent_name") if lifecycle else ()),
                 ("task_name", "name"))
    if lifecycle:
        agent_id = str(first_attr(attrs, "gen_ai.client.agent_id", "gen_ai.agent.id", "subagent.id", "agent_id") or "")
    else:
        # gen_ai.agent.id on a tool belongs to the caller, not the new child.
        agent_id = str(output.get("agent_id") or payload.get("agent_id") or attrs.get("subagent.id") or "")
    failed = not lifecycle and tool_failure(event, dict(attrs)) is not None
    return SubagentFact(lifecycle or "SubagentStart", role, name, agent_id, recorded_tool_call_id(attrs), failed)


def extract_subagent_name_from_tool(
    tool_name: object,
    attrs: Mapping[str, object] | None = None,
    preview: str = "",
) -> str:
    if not is_subagent_launch_tool(tool_name):
        return ""
    fact = subagent_fact(attrs or {}, tool_name=tool_name, preview=preview)
    return fact.label if fact and (not fact.failed or fact.agent_id) else ""


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


def _load_json_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
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
