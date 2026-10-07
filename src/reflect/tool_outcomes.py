"""Conservative, provider-independent failure evidence from tool results.

Only execution metadata and recognized outer envelopes are authoritative. Never
search arbitrary stdout, nested application data, or quoted content for errors.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from reflect.utils import _json_loads


@dataclass(frozen=True)
class ToolFailure:
    error_type: str
    message: str


_SCRIPT_FAILURE = re.compile(r"\AScript failed\nWall time [0-9.]+ seconds\nOutput:\n")
_RESULT_EVENTS = {"posttooluse", "posttoolusefailure", "aftermcpexecution", "aftershellexecution"}


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(
            block["text"] for block in value
            if isinstance(block, dict) and block.get("type") in ("text", "input_text")
            and isinstance(block.get("text"), str)
        )
    return ""


def _result_failure(output: object) -> ToolFailure | None:
    if isinstance(output, str) and output.lstrip().startswith(("{", "[")):
        try:
            output = _json_loads(output)
        except (ValueError, TypeError):
            return None
    if isinstance(output, dict):
        if output.get("isError") is True or output.get("is_error") is True:
            message = _text(output.get("content")) or _text(output.get("error"))
            return ToolFailure("tool_error", message or "Tool result reports an error")
        code = output.get("exit_code")
        if isinstance(code, int) and not isinstance(code, bool) and code != 0:
            return ToolFailure("process_exit", f"Process exited with code {code}")
        return None
    text = _text(output)
    match = _SCRIPT_FAILURE.match(text)
    if match:
        detail = text[match.end():].lstrip("\n")
        if detail.startswith("Script error:\n"):
            return ToolFailure("execution_error", detail.removeprefix("Script error:\n").strip())
    return None


def tool_failure(event: str, attrs: dict[str, Any]) -> ToolFailure | None:
    """Classify a terminal tool event, preserving explicit provider outcomes.

    Explicit statuses take precedence over output inference. A result wrapper
    without such a status can supply a failure; an invocation cannot.
    """
    event = str(attrs.get("gen_ai.client.hook.event") or attrs.get("ide.hook.event")
                or attrs.get("event.name") or event).rsplit(".", 1)[-1].lower()
    if event not in _RESULT_EVENTS:
        return None
    status = str(attrs.get("gen_ai.client.status") or attrs.get("status") or "").lower()
    message = _text(attrs.get("gen_ai.client.error.text")) or _text(attrs.get("error.message"))
    error_type = _text(attrs.get("error.type"))
    if status in {"error", "failed", "failure"} or event == "posttoolusefailure" or error_type:
        return ToolFailure(error_type or "tool_error", message or "Tool execution failed")
    if status in {"ok", "success", "succeeded", "complete", "completed", "skipped"}:
        return None
    code = attrs.get("process.exit.code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return ToolFailure("process_exit", f"Process exited with code {code}")
    output = attrs.get("gen_ai.client.tool.output", attrs.get("tool.output"))
    return _result_failure(output)


def with_tool_outcome(event: str, attrs: dict[str, Any]) -> dict[str, Any]:
    """Add canonical failure attributes without mutating captured evidence."""
    failure = tool_failure(event, attrs)
    if failure is None:
        return attrs
    return {**attrs, "gen_ai.client.status": "error", "error.type": failure.error_type,
            "gen_ai.client.error.text": failure.message}
