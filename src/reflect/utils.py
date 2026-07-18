from __future__ import annotations

import json as _json_stdlib
import logging
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger("reflect")

try:
    import orjson as _orjson
    _json_loads = _orjson.loads

    def _json_dumps(o) -> str:  # type: ignore[misc]
        return _orjson.dumps(o).decode()
except ImportError:
    _json_loads = _json_stdlib.loads
    _json_dumps = _json_stdlib.dumps


def _load_json_lines(file_path: Path) -> Iterable[dict]:
    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = _json_loads(line)
            except (ValueError, _json_stdlib.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload


def _flatten_text_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _safe_ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _fmt_model(m: str) -> str:
    """claude-sonnet-4-6-20241022 → sonnet-4-6"""
    m = re.sub(r"^claude-", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    return m


def _fmt_dur(ms: float) -> str:
    if ms <= 0:
        return "—"
    if ms < 1000:
        return f"{ms:.0f}ms"
    if ms < 60_000:
        return f"{ms/1000:.0f}s"
    return f"{int(ms//60000)}m{int(ms/1000)%60:02d}s"


def _bar(filled: int, total: int, color: str, empty: str = "grey23"):
    from rich.text import Text
    return Text("█" * filled, style=color) + Text("░" * (total - filled), style=empty)


def _stat_panel(label: str, value: str, color: str):
    from rich.panel import Panel
    from rich.text import Text
    return Panel(
        Text(value, style=f"bold {color}", justify="center"),
        title=f"[dim]{label}[/]",
        border_style=color,
        padding=(0, 2),
    )


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _redact_path_token(token: str) -> str:
    if not isinstance(token, str):
        return ""
    normalized = token.replace("\\", "/").strip()
    if not normalized:
        return ""

    basename = normalized.rstrip("/").split("/")[-1] if normalized.rstrip("/") else ""

    if normalized.startswith("~/"):
        return f"~/{basename}" if basename else "~/<path>"

    home_dir = Path.home().as_posix()
    if normalized == home_dir:
        return "~"
    if normalized.startswith(f"{home_dir}/"):
        return f"~/{basename}" if basename else "~/<path>"

    if normalized.startswith("/private/var/folders/") or normalized.startswith("/tmp/"):
        return f"<tmp>/{basename}" if basename else "<tmp>"

    if normalized.startswith("/"):
        return f"<path>/{basename}" if basename else "<path>"

    return token


def _sanitize_command_display(command: str, *, max_len: int | None = None) -> str:
    if not isinstance(command, str):
        return ""

    secret_name = r"[A-Z_][A-Z0-9_]*(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|COOKIE)[A-Z0-9_]*"

    def _redact_assignment(match: re.Match[str]) -> str:
        quote = match.group("quote") or ""
        return f"{match.group('prefix')}{quote}[REDACTED]{quote}"

    sanitized = re.sub(
        rf"(?i)(?P<prefix>\b(?:export\s+)?{secret_name}\s*=\s*)(?P<quote>[\"']?)(?P<value>[^\s;&|\"']+)(?P=quote)",
        _redact_assignment,
        command,
    )
    sanitized = re.sub(
        r"(?i)(?P<prefix>--(?:api[-_]?key|token|secret|password|passwd|credential|authorization)(?:=|\s+))(?P<quote>[\"']?)(?P<value>[^\s;&|\"']+)(?P=quote)",
        _redact_assignment,
        sanitized,
    )
    sanitized = re.sub(
        r"(?i)(\bAuthorization\s*:\s*(?:Bearer|Basic)\s+)[^\s\"']+",
        r"\1[REDACTED]",
        sanitized,
    )

    def _replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        token = match.group("token")
        return f"{quote}{_redact_path_token(token)}{quote}"

    sanitized = re.sub(
        r'(?P<quote>["\'])(?P<token>(?:~|/)[^"\']+)(?P=quote)',
        _replace_quoted,
        sanitized,
    )
    sanitized = re.sub(
        r'(?P<token>(?:~|/)[^\s"\']+)',
        lambda match: _redact_path_token(match.group("token")),
        sanitized,
    )
    sanitized = sanitized.strip()
    if max_len is not None and len(sanitized) > max_len:
        return sanitized[:max_len] + "..."
    return sanitized


def _sanitize_command_counter(commands: Counter[str] | dict[str, int]) -> Counter[str]:
    sanitized: Counter[str] = Counter()
    for command, count in commands.items():
        label = _sanitize_command_display(str(command))
        if not label:
            continue
        sanitized[label] += int(count)
    return sanitized
