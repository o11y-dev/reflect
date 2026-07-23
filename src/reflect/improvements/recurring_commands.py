from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_COMMAND_VALUE_KEYS = {
    "command",
    "condition",
    "goal",
    "message",
    "objective",
    "prompt",
    "text",
}
_CONTROL_ARGUMENTS = {
    "",
    "cancel",
    "clear",
    "list",
    "none",
    "off",
    "pause",
    "reset",
    "resume",
    "status",
    "stop",
}
_WAKE_RE = re.compile(r"\bAGENT_LOOP_(?:WAKE_)?([A-Za-z0-9][A-Za-z0-9_-]{0,80})\b")
_SLASH_RE = re.compile(r"^/(loop|goal|every|schedule)\b(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_CADENCE_RE = re.compile(r"^(?:every\s+)?(?:\d+\s*)?(?:s|sec|secs|seconds?|m|min|mins|minutes?|h|hours?|d|days?)\b[\s,:-]*", re.IGNORECASE)
_TOOL_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _tool_token(value: str) -> str:
    return _TOOL_TOKEN_RE.sub("_", value.lower()).strip("_")


def _agent_family(value: str) -> str:
    normalized = value.lower()
    if "cursor" in normalized:
        return "cursor"
    if "claude" in normalized:
        return "claude"
    if "copilot" in normalized:
        return "copilot"
    if "codex" in normalized or "openai" in normalized:
        return "codex"
    if "gemini" in normalized:
        return "gemini"
    if "windsurf" in normalized:
        return "windsurf"
    if "opencode" in normalized:
        return "opencode"
    return normalized.strip() or "unknown"


def _command_values(preview: str | None) -> list[str]:
    if not preview:
        return []
    stripped = preview.strip()
    values: list[str] = []
    if stripped.startswith("/"):
        values.append(stripped)
    try:
        payload = json.loads(stripped)
    except (TypeError, json.JSONDecodeError):
        return values

    def visit(value: Any, key: str | None = None) -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key).lower())
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif isinstance(value, str) and key in _COMMAND_VALUE_KEYS:
            values.append(value.strip())

    visit(payload)
    return values


def _objective_label(value: str, *, fallback: str) -> str:
    compact = " ".join(value.split()).strip(" \t\r\n\"'")
    compact = _CADENCE_RE.sub("", compact)
    return (compact[:77] + "…") if len(compact) > 78 else (compact or fallback)


@dataclass(frozen=True)
class RecurringCommandMatch:
    """One strong, privacy-safe agent-native continuation signal."""

    agent_family: str
    family: str
    command: str
    identity: str
    label: str
    confidence: float
    signal: str


class RecurringCommandRegistry:
    """Classify documented recurring commands without matching incidental prose."""

    _supported_slash_commands = {
        "cursor": {"loop"},
        "claude": {"loop", "goal", "schedule"},
        "copilot": {"loop", "every"},
        "codex": {"goal"},
    }

    def classify(
        self,
        *,
        agent_name: str,
        tool_name: str | None,
        preview: str | None,
    ) -> RecurringCommandMatch | None:
        agent = _agent_family(agent_name)
        tool = _tool_token(tool_name or "")
        preview_text = preview or ""

        wake = _WAKE_RE.search(preview_text)
        if agent == "cursor" and wake:
            slug = wake.group(1).lower()
            return self._match(
                agent=agent,
                family="scheduled_loop",
                command="/loop",
                objective=slug.replace("_", " ").replace("-", " "),
                identity=slug,
                confidence=0.98,
                signal="cursor_wake_sentinel",
            )

        if agent in {"claude", "codex"} and tool.endswith("create_goal"):
            objective = self._first_objective(preview_text) or "durable goal"
            return self._match(
                agent=agent,
                family="goal",
                command="/goal",
                objective=objective,
                confidence=0.98,
                signal="native_goal_tool",
            )

        if agent == "claude" and tool.endswith("croncreate") and self._is_recurring_cron(preview_text):
            objective = self._first_objective(preview_text) or "scheduled prompt"
            return self._match(
                agent=agent,
                family="scheduled_loop",
                command="CronCreate",
                objective=objective,
                confidence=0.97,
                signal="native_recurring_cron_tool",
            )

        for value in _command_values(preview_text):
            slash = _SLASH_RE.match(value.strip())
            if slash is None:
                continue
            command = slash.group(1).lower()
            objective = (slash.group(2) or "").strip()
            if command not in self._supported_slash_commands.get(agent, set()):
                continue
            if command == "goal" and objective.lower() in _CONTROL_ARGUMENTS:
                continue
            if command == "schedule" and not self._looks_recurring_schedule(objective):
                continue
            family = "goal" if command == "goal" else "scheduled_loop"
            return self._match(
                agent=agent,
                family=family,
                command=f"/{command}",
                objective=objective or f"{command} maintenance",
                confidence=0.94,
                signal="documented_slash_command",
            )
        return None

    def _match(
        self,
        *,
        agent: str,
        family: str,
        command: str,
        objective: str,
        confidence: float,
        signal: str,
        identity: str | None = None,
    ) -> RecurringCommandMatch:
        label = _objective_label(objective, fallback=family.replace("_", " "))
        stable_identity = identity or _short_hash(" ".join(objective.lower().split()))
        return RecurringCommandMatch(
            agent_family=agent,
            family=family,
            command=command,
            identity=stable_identity,
            label=label,
            confidence=confidence,
            signal=signal,
        )

    @staticmethod
    def _first_objective(preview: str) -> str | None:
        values = _command_values(preview)
        if values:
            value = values[0]
            slash = _SLASH_RE.match(value)
            return (slash.group(2) or "").strip() if slash else value
        return None

    @staticmethod
    def _is_recurring_cron(preview: str) -> bool:
        try:
            payload = json.loads(preview)
        except (TypeError, json.JSONDecodeError):
            return bool(re.search(r"\b(recurring|repeat|repeats)\b", preview, re.IGNORECASE))
        serialized = json.dumps(payload, sort_keys=True).lower()
        return bool(re.search(r'"(?:recurring|repeat|repeats)"\s*:\s*true', serialized))

    @staticmethod
    def _looks_recurring_schedule(objective: str) -> bool:
        return bool(
            re.search(
                r"\b(?:every|hourly|daily|weekdays?|weekly|monthly|cron|recurring|repeat)\b",
                objective,
                re.IGNORECASE,
            )
        )
