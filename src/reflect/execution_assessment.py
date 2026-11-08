"""Conservative, chronological assessment of already captured execution evidence.

This module never interprets an agent's completion report as an observation.
Tool names and prose mentions alone cannot prove that a check ran successfully.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

ASSESSMENT_VERSION = 1


def attrs_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        result = json.loads(str(value or "{}"))
    except (ValueError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def command_from_tool(tool: dict[str, Any]) -> str:
    attrs = attrs_dict(tool.get("raw_attrs_json"))
    for key in ("gen_ai.client.command", "ide.command", "command", "shell.command"):
        if isinstance(attrs.get(key), str):
            return attrs[key]
    payload = attrs_dict(attrs.get("gen_ai.client.tool.input")) or attrs_dict(
        tool.get("input_preview_redacted")
    )
    command = payload.get("cmd") or payload.get("command")
    if isinstance(command, str):
        return command
    if str(tool.get("tool_name") or "").lower() in {"exec", "exec_command", "bash", "shell"}:
        preview = tool.get("input_preview_redacted")
        if isinstance(preview, str) and not preview.lstrip().startswith(("{", "[")):
            return preview
    return ""


def check_command(command: str) -> str | None:
    """Recognize an actual single check invocation, not search text or a script."""
    # shlex builds quoted tokens character by character. Captured scripts can
    # contain megabytes of embedded data; leave them unassessed, never truncate.
    if len(command) > 32 * 1024 or any(char in command for char in ";&|<>\n`") or "$(" in command:
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    identity_tokens = list(tokens)
    if not tokens:
        return None
    while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
        tokens.pop(0)
    if tokens[:2] in (["poetry", "run"], ["uv", "run"]):
        tokens = tokens[2:]
    if tokens and PurePosixPath(tokens[0]).name.startswith("python"):
        if len(tokens) < 3 or tokens[1] != "-m":
            return None
        tokens = tokens[2:]
    if not tokens:
        return None
    executable = PurePosixPath(tokens[0]).name
    is_check = executable in {"pytest", "unittest", "mypy", "pyright"}
    is_check |= executable == "ruff" and tokens[1:2] == ["check"]
    is_check |= executable in {"npm", "pnpm", "yarn"} and tokens[1:2] in (
        ["test"], ["lint"], ["build"],
    )
    is_check |= executable in {"cargo", "go"} and tokens[1:2] in (["test"], ["check"])
    if not is_check or any(token in {"--help", "-h", "--version", "--collect-only"} for token in tokens):
        return None
    return shlex.join(identity_tokens)


def explicit_exit(tool: dict[str, Any]) -> int | None:
    attrs = attrs_dict(tool.get("raw_attrs_json"))
    code = attrs.get("process.exit.code", attrs.get("exit_code"))
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    output = attrs.get("gen_ai.client.tool.output", attrs.get("tool.output"))
    output = attrs_dict(output) or attrs_dict(tool.get("output_preview_redacted"))
    code = output.get("exit_code")
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    # The harness process-result envelope, not arbitrary lines in tool stdout.
    preview = str(tool.get("output_preview_redacted") or "")
    match = re.match(r"\A(?:Chunk ID: [^\n]+\n)?Wall time: [^\n]+\nProcess exited with code (-?\d+)\n", preview)
    return int(match[1]) if match else None


def edit_paths(tool: dict[str, Any]) -> set[str] | None:
    """None means no edit, an empty set means an edit with unknown scope."""
    name = str(tool.get("tool_name") or "").lower().rsplit("__", 1)[-1]
    if name != "write" and not any(part in name for part in ("apply_patch", "edit", "write_file", "writefile")):
        return None
    attrs = attrs_dict(tool.get("raw_attrs_json"))
    payload = attrs_dict(attrs.get("gen_ai.client.tool.input")) or attrs_dict(
        tool.get("input_preview_redacted")
    )
    paths = {
        str(value) for value in (
            attrs.get("gen_ai.client.file_path"), payload.get("file_path"), payload.get("path"),
        ) if isinstance(value, str) and value
    }
    patch = str(payload.get("patch") or payload.get("input") or tool.get("input_preview_redacted") or "")
    paths.update(re.findall(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", patch, re.MULTILINE))
    return paths


@dataclass(frozen=True, slots=True)
class CheckObservation:
    identity: str
    step_id: str
    at: str
    started_at: str
    exit_code: int | None
    scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EditObservation:
    step_id: str
    at: str
    paths: frozenset[str]


def assess_execution(
    *,
    tools: list[dict[str, Any]],
    edits: list[EditObservation],
    closed: bool,
    input_identity: list[object],
) -> dict[str, Any]:
    checks: dict[str, CheckObservation] = {}
    pending: dict[str, str] = {}
    for tool in tools:
        command = check_command(command_from_tool(tool))
        if command is None:
            continue
        attrs = attrs_dict(tool.get("raw_attrs_json"))
        scope = attrs.get("reflect.check.scope")
        # A test command without an explicit narrower dependency scope covers
        # the workspace conservatively; file arguments are not a dependency map.
        scope = tuple(str(p) for p in scope) if isinstance(scope, list) else (".",)
        observation = CheckObservation(
            identity=command,
            step_id=str(tool["step_id"]),
            at=str(tool.get("ended_at") or tool.get("started_at") or ""),
            started_at=str(tool.get("started_at") or tool.get("ended_at") or ""),
            exit_code=explicit_exit(tool),
            scope=scope,
        )
        previous = checks.get(command)
        if previous is None or observation.at >= previous.at:
            if observation.exit_code is None:
                pending[command] = observation.step_id
                if previous is not None and previous.exit_code is not None:
                    continue
            else:
                pending.pop(command, None)
            checks[command] = observation

    invalidated: dict[str, list[str]] = {}
    for check in checks.values():
        if check.exit_code is None:
            continue
        for edit in edits:
            if edit.at < check.started_at:
                continue
            if not edit.paths or "." in check.scope or any(
                path == scope or path.startswith(scope.rstrip("/") + "/")
                or scope.startswith(path.rstrip("/") + "/")
                for path in edit.paths for scope in check.scope
            ):
                invalidated.setdefault(check.step_id, []).append(edit.step_id)
    failed = any(check.exit_code is not None and check.exit_code != 0 and check.step_id not in invalidated
                 for check in checks.values())
    unknown = bool(pending) or any(check.exit_code is None for check in checks.values())
    state = (
        "failed" if failed else "invalidated" if invalidated else
        "unknown" if unknown or not checks else "passed"
    )
    payload = {
        "version": ASSESSMENT_VERSION,
        "boundary": "closed" if closed else "open_or_ambiguous",
        "verification": state,
        "outcome": "unknown",
        "exposure": "unknown",
        "reasons": ["task_acceptance_not_observed"],
        "checks": [
            {"step_id": check.step_id, "at": check.at, "started_at": check.started_at, "exit_code": check.exit_code,
             "scope": list(check.scope), "invalidated": check.step_id in invalidated,
             "invalidated_by": sorted(set(invalidated.get(check.step_id, [])))}
            for check in checks.values()
        ],
        "check_count": len(checks),
        "terminal_check_count": sum(check.exit_code is not None for check in checks.values()),
        "pending_check_step_ids": sorted(pending.values()),
    }
    payload["input_digest"] = hashlib.sha256(
        json.dumps([input_identity, payload, [(edit.step_id, edit.at, sorted(edit.paths)) for edit in edits]],
                   sort_keys=True, default=str).encode()
    ).hexdigest()
    return payload
