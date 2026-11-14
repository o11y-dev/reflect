"""Read literal tool invocations from captured inputs; never execute captured code.

Only straight-line ``text(await tools.name(literal));`` wrappers are supported.
Computed arguments, branches, loops and incomplete captures remain opaque. Child
results are paired only when the complete output contains one result per call.
Derived observations still reference the owning canonical tool call.
"""
from __future__ import annotations

import json
import re
import shlex
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any

_LIMIT = 131_072
_START = re.compile(r'\s*text\(\s*await\s+tools\.([A-Za-z_]\w*)\s*\(')
_END = re.compile(r'\s*\)\s*\)\s*;?')
_KEY = re.compile(r'[A-Za-z_]\w*')
_DECODER = json.JSONDecoder()


def _object(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or ''))
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError):
        return {}


def _literal(code: str, position: int) -> tuple[Any, int]:
    position += len(code[position:]) - len(code[position:].lstrip())
    if code[position:position + 1] != '{':
        return _DECODER.raw_decode(code, position)
    position += 1
    result = {}
    while True:
        position += len(code[position:]) - len(code[position:].lstrip())
        if code[position:position + 1] == '}':
            return result, position + 1
        match = _KEY.match(code, position)
        if match:
            key, position = match.group(), match.end()
        else:
            key, position = _DECODER.raw_decode(code, position)
            if not isinstance(key, str):
                raise ValueError('non-string key')
        position += len(code[position:]) - len(code[position:].lstrip())
        if code[position:position + 1] != ':' or key in result:
            raise ValueError('invalid object')
        value, position = _literal(code, position + 1)
        result[key] = value
        position += len(code[position:]) - len(code[position:].lstrip())
        if code[position:position + 1] == '}':
            return result, position + 1
        if code[position:position + 1] != ',':
            raise ValueError('invalid object delimiter')
        position += 1


def _results(value: object) -> list[dict]:
    if isinstance(value, str):
        with suppress(ValueError):
            value = json.loads(value)
    if isinstance(value, list):
        value = '\n'.join(str(item.get('text', '')) for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        return [value]
    text = str(value or '')
    if text.startswith('Script completed') and '\nOutput:\n' in text:
        text = text.split('\nOutput:\n', 1)[1]
    values = []
    while text.strip():
        try:
            result, end = _DECODER.raw_decode(text.lstrip())
        except ValueError:
            return []
        if not isinstance(result, dict):
            return []
        values.append(result)
        text = text.lstrip()[end:]
    return values


def tool_invocations(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Return direct input or explicit wrapper children with isolated results."""
    attrs = _object(row.get('raw_attrs_json'))
    code = attrs.get('gen_ai.client.tool.input') or row.get('input_preview_redacted')
    if str(row.get('tool_name') or '').rsplit('__', 1)[-1].rsplit('.', 1)[-1] != 'exec' or not isinstance(code, str) or len(code) > _LIMIT:
        return [row]
    calls, position = [], 0
    try:
        while code[position:].strip():
            match = _START.match(code, position)
            if not match:
                return [row]
            arguments, end = _literal(code, match.end())
            suffix = _END.match(code, end)
            if not suffix:
                return [row]
            calls.append((match.group(1), arguments))
            position = suffix.end()
    except (ValueError, RecursionError):
        return [row]
    if not calls:
        return [row]
    results = _results(attrs.get('gen_ai.client.tool.output') or row.get('output_preview_redacted'))
    if len(results) != len(calls):
        return [row]
    projected = []
    for index, (name, arguments) in enumerate(calls):
        result = results[index]
        code = result.get('exit_code')
        code = code if isinstance(code, int) and not isinstance(code, bool) else None
        child_attrs = {key: value for key, value in attrs.items() if not any(part in key for part in ('tool.', 'command', 'exit', 'error'))}
        child_attrs['gen_ai.client.tool.input'] = arguments
        child_attrs['reflect.tool.wrapper_index'] = index
        if code is not None:
            child_attrs['process.exit.code'] = code
        projected.append({**row, 'tool_name': name, 'raw_attrs_json': child_attrs,
                          'input_preview_redacted': json.dumps(arguments),
                          'output_preview_redacted': json.dumps(result) if result else None,
                          'status': 'ok' if code == 0 else 'error' if code is not None else 'unknown',
                          'wrapper_index': index})
    return projected


def skill_read_paths(attrs: dict, tool_name: str) -> list[tuple[str, bool]]:
    """Explicit read paths and whether a command result covers that read alone."""
    payload = _object(attrs.get('gen_ai.client.tool.input'))
    path = attrs.get('gen_ai.client.file_path') or payload.get('file_path') or payload.get('path')
    name = str(tool_name).lower().rsplit('__', 1)[-1]
    if 'read' in name and isinstance(path, str) and PurePosixPath(path).name == 'SKILL.md':
        return [(path, True)]
    command = attrs.get('gen_ai.client.command') or attrs.get('command') or attrs.get('shell.command') or payload.get('cmd') or payload.get('command')
    if name not in {'exec', 'exec_command', 'bash', 'shell'} or not isinstance(command, str) or len(command) > 32768 or any(t in command for t in ('<', '>', '$', '`')):
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=';&|\n')
        lexer.whitespace = ' \t\r'
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    segments, current = [], []
    for token in tokens:
        if token and all(char in ';&|\n' for char in token):
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return [(token, len(segments) == 1) for segment in segments
            if segment and PurePosixPath(segment[0]).name in {'cat', 'head', 'tail', 'bat'}
            for token in segment[1:] if PurePosixPath(token).name == 'SKILL.md' and not token.startswith('-')]


def resolve_skill_path(path: str, attrs: dict, workspace: str) -> str:
    """Resolve captured relative paths against the invocation directory first."""
    if not path:
        return ''
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        payload = _object(attrs.get('gen_ai.client.tool.input'))
        directory = payload.get('workdir') or attrs.get('gen_ai.client.cwd') or workspace
        if not directory or not Path(str(directory)).expanduser().is_absolute():
            return ''
        candidate = Path(str(directory)).expanduser() / candidate
    return str(candidate.resolve())
