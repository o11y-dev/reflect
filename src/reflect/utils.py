from __future__ import annotations

import json as _json_stdlib
import logging
import re
from collections import Counter
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

    def _replace_quoted(match: re.Match[str]) -> str:
        quote = match.group("quote")
        token = match.group("token")
        return f"{quote}{_redact_path_token(token)}{quote}"

    sanitized = re.sub(
        r'(?P<quote>["\'])(?P<token>(?:~|/)[^"\']+)(?P=quote)',
        _replace_quoted,
        command,
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
