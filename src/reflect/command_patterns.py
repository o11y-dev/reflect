"""Provider-neutral shell evidence and bounded, operand-redacted command templates.

Templates describe captured syntax, not successful execution. Complex scripts
remain opaque and fingerprinted instead of masquerading as their first command.
"""
from __future__ import annotations

import hashlib
import re
import shlex
from collections.abc import Mapping
from functools import lru_cache
from pathlib import PurePosixPath

from reflect.utils import _json_loads

MAX_COMMAND_LENGTH = 32 * 1024
COMMAND_ATTRIBUTES = ("gen_ai.client.command", "ide.command", "command", "shell.command", "process.command_line")
SHELL_TOOLS = frozenset({
    "shell", "bash", "sh", "zsh", "exec_command", "shell_command", "run_shell_command",
    "run_command", "execute_command", "run_in_terminal", "shell_exec", "terminal_exec", "powershell",
})
# exec is also a JavaScript orchestration tool; only its structured cmd/command
# envelope is shell evidence. Never inspect arbitrary JavaScript source.
STRUCTURED_SHELL_TOOLS = SHELL_TOOLS | {"exec"}
_INPUT_ATTRIBUTES = ("gen_ai.client.tool.input", "tool.input")
_VALUE_OPTIONS = frozenset({
    "--config", "--cwd", "--format", "--output", "--profile", "--project", "--workspace",
    "--repo", "--repository", "--hostname", "--state", "--limit", "--json", "--jq",
    "--namespace", "--context", "--selector", "--file", "--directory", "--max-count",
    "--glob", "--include", "--exclude", "--tb", "--maxfail", "--rootdir", "--cov",
    "--token", "--password", "--api-key", "-o", "-f", "-k", "-m", "-R",
})
_ACTION_DEPTH = {"git": 1, "gh": 2, "glab": 2, "rtk": 2, "npm": 1, "pnpm": 1,
                 "yarn": 1, "cargo": 1, "go": 1, "docker": 1, "kubectl": 1,
                 "pip": 1, "uv": 1, "poetry": 1, "reflect": 1, "alembic": 1}
_ACTION = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")


def _object(value: object) -> Mapping:
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str) or not value.lstrip().startswith("{"):
        return {}
    try:
        payload = _json_loads(value)
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, Mapping) else {}


def shell_tool_name(value: object) -> str:
    return str(value or "").lower().rsplit(".", 1)[-1]


def _command_value(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return shlex.join(value)
    return ""


def extract_command(attrs: object, preview: object = "", *, allow_text: bool = True) -> str:
    """Prefer current explicit/input attributes to a potentially stale preview."""
    attributes = _object(attrs)
    for key in COMMAND_ATTRIBUTES:
        command = _command_value(attributes.get(key))
        if command:
            return command
    for value in [*(attributes.get(key) for key in _INPUT_ATTRIBUTES), preview]:
        payload = _object(value)
        for key in ("cmd", "command", "command_line", "CommandLine"):
            command = _command_value(payload.get(key))
            if command:
                return command
        if allow_text and isinstance(value, str) and value.strip() and not value.lstrip().startswith(("{", "[")):
            return value.strip()
    return ""


def shell_command(tool_name: object, attrs: object, preview: object = "") -> str:
    """Require shell tool identity or an explicit command attribute."""
    attributes = _object(attrs)
    name = shell_tool_name(tool_name)
    if name == "exec":
        attributes = {key: value for key, value in attributes.items() if key not in COMMAND_ATTRIBUTES or key in {"shell.command", "process.command_line"}}
    if name not in STRUCTURED_SHELL_TOOLS and not any(attributes.get(key) for key in COMMAND_ATTRIBUTES):
        return ""
    return extract_command(attributes, preview, allow_text=name in SHELL_TOOLS)


def command_projection(tool_name: object, attrs: object, preview: object = "") -> tuple[str | None, bool]:
    command = shell_command(tool_name, attrs, preview)
    return (command_pattern(command) if command else None,
            bool(command) or shell_tool_name(tool_name) in SHELL_TOOLS)


def _script_pattern(command: str) -> str:
    return "shell script [" + hashlib.sha256(command.encode()).hexdigest()[:12] + "]"


def command_pattern(command: str) -> str:
    if not command.strip():
        return ""
    if len(command) > MAX_COMMAND_LENGTH:
        return _script_pattern(command)
    return _pattern(command.strip())


def shell_command_parts(command: str, *, reject_groups: bool = False) -> list[str]:
    """Split only unquoted shell operators; leave word parsing to shlex."""
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif reject_groups and char in "(){}":
            raise ValueError("grouped shell commands")
        elif char == "#" and (index == 0 or command[index - 1].isspace()):
            raise ValueError("comment-bearing script")
        elif char in ";&|<>\n":
            before = command[start:index].strip()
            descriptor = ""
            if char in "<>":
                match = re.search(r"(?<!\S)\d+$", before)
                if match:
                    descriptor = match[0]
                    before = before[:match.start()].strip()
            if before:
                parts.append(before)
            end = index + 1
            while end < len(command) and command[end] == char and char != "\n":
                end += 1
            if char in "<>" and end < len(command) and command[end] == "&":
                match = re.match(r"&(?:\d+|-)", command[end:])
                if match:
                    end += len(match[0])
            parts.append(";" if char == "\n" else descriptor + command[index:end])
            start = end
            index = end - 1
        index += 1
    if command[start:].strip():
        parts.append(command[start:].strip())
    return parts


def _words(value: str) -> list[str]:
    return shlex.split(value) if any(char in value for char in "\"'\\") else value.split()


@lru_cache(maxsize=1024)
def _pattern(command: str, depth: int = 0) -> str:
    if depth > 4 or any(marker in command for marker in ("$(", "`", "<<")):
        return _script_pattern(command)
    result: list[str] = []
    try:
        for part in shell_command_parts(command):
            if part in {";", "&&", "||", "|", "&"} or re.fullmatch(r"\d*[<>]{1,2}(?:&(?:\d+|-))?", part):
                result.append(part)
                continue
            if result and re.fullmatch(r"\d*[<>]{1,2}", result[-1]):
                words = _words(part)
                result.append("<path>")
                if len(words) > 1:
                    result.extend(_operand(word) for word in words[1:])
                continue
            words = _words(part)
            if len(words) > 512 or (words and words[0] in {"if", "for", "while", "case", "function", "{"}):
                return _script_pattern(command)
            result.append(_simple_pattern(words, depth))
            if sum(map(len, result)) > 240:
                return _script_pattern(command)
    except ValueError:
        return _script_pattern(command)
    pattern = " ".join(item for item in result if item)
    return pattern if len(pattern) <= 240 else _script_pattern(command)


def _operand(value: str) -> str:
    return "<path>" if "/" in value or "\\" in value or re.search(r"\.[A-Za-z][A-Za-z0-9]*$", value) else "<arg>"


def _simple_pattern(words: list[str], depth: int) -> str:
    if depth > 4:
        return _script_pattern(shlex.join(words))
    while words and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[0]):
        words = words[1:]
    if not words:
        return "env <assignment>"
    executable = PurePosixPath(words[0]).name
    args = words[1:]
    if executable == "env":
        return _simple_pattern(args, depth + 1) if args else "env"
    if executable in {"poetry", "uv"} and args[:1] == ["run"]:
        return _simple_pattern(args[1:], depth + 1)
    if executable in {"bash", "sh", "zsh"} and len(args) == 2 and args[0] in {"-c", "-lc", "-ic"}:
        return _pattern(args[1], depth + 1)
    if re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", executable):
        if args[:1] == ["-m"] and len(args) > 1:
            module = args[1]
            nested = _simple_pattern([module, *args[2:]], depth + 1)
            return nested if module in {"pytest", "unittest"} else "python -m " + nested
        if args[:1] == ["-c"]:
            return "python -c <code>" + "".join(" " + _operand(arg) for arg in args[2:])
        return "python" + (" <script>" if args else "") + "".join(" " + _operand(arg) for arg in args[1:])
    if not _ACTION.fullmatch(executable):
        return "<executable> " + " ".join(_operand(arg) for arg in args)
    result = [executable]
    actions = 0
    max_actions = _ACTION_DEPTH.get(executable, 0)
    index = 0
    while index < len(args):
        arg = args[index]
        if executable == "git" and actions == 0 and arg in {"-C", "-c", "--git-dir", "--work-tree"}:
            index += 2
            continue
        if arg.startswith("-"):
            option, separator, _value = arg.partition("=")
            has_value = option in _VALUE_OPTIONS or (executable == "rtk" and option in {"-c", "--project"})
            if executable != "rtk":
                result.append(option + ("=<value>" if separator else ""))
                if has_value and not separator and index + 1 < len(args):
                    result.append("<value>")
            index += 2 if has_value and not separator else 1
            continue
        if actions < max_actions and _ACTION.fullmatch(arg):
            result.append(arg)
            actions += 1
            if actions == 1 and ((executable in {"npm", "pnpm", "yarn"} and arg == "run") or (executable == "uv" and arg == "pip")):
                max_actions = 2
        elif executable != "rtk":
            result.append(_operand(arg))
        index += 1
    return " ".join(result)
