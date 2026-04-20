#!/usr/bin/env python3
"""
Generate a structured AI usage telemetry report.

Reads telemetry from the canonical local OTLP traces cache first
(`~/.reflect/state/otlp/otel-traces.json`).

When the default local paths are in use, reflect can also normalize either:
  1. Local JSONL hook spans from `.state/local_spans/`
  2. Rich local session stores such as Copilot `events.jsonl`, Claude project
     transcripts, and Gemini chat session JSON

into that same local OTLP cache before analysis.

When available, a sibling OTLP logs file (`otel-logs.json`) is also read as a
secondary enrichment source to fill missing per-session model metadata.

Usage:
    # From collector file exporter (recommended)
    python3 src/reflect/core.py \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json

    # Save a markdown report instead of the terminal dashboard
    python3 src/reflect/core.py \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json --no-terminal

    # Open the local dashboard in a browser
    python3 src/reflect/core.py report \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json

    # From local hook state (legacy)
    python3 src/reflect/core.py \\
        --sessions-dir ".cursor/hooks/opentelemetry-hook/.state/sessions" \\
        --spans-dir ".cursor/hooks/opentelemetry-hook/.state/local_spans"
"""

from __future__ import annotations

import io
import json as _json_stdlib
import os
import platform
import re
import shutil
import subprocess
import tomllib
import zipfile
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import click

# ---------------------------------------------------------------------------
# Reflect home directory
# ---------------------------------------------------------------------------

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
HOOK_HOME = Path(os.environ.get("IDE_OTEL_HOOK_HOME",
                                 Path.home() / ".local" / "share" / "opentelemetry-hooks"))
_HOOK_PACKAGE_SPEC = "opentelemetry-hooks==0.11.0"
_HOOK_CFG_ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"
_HOOK_CFG_ENDPOINT_DEFAULT = "http://localhost:4317"
_HOOK_CFG_PROTOCOL_KEY = "OTEL_EXPORTER_OTLP_PROTOCOL"
_HOOK_CFG_PROTOCOL_DEFAULT = "grpc"

# ---------------------------------------------------------------------------
# Re-exports from split modules — keeps backward compatibility for serve.py,
# tests, and any external consumers that import from reflect.core.
# ---------------------------------------------------------------------------

from reflect.dashboard import (  # noqa: F401
    _artifact_report_ref,
    _build_dashboard_json,
    _start_publish_server,
    _start_publish_server_inline,
    _update_dashboard_data,
    _write_dashboard_artifact,
)
from reflect.graph import (  # noqa: F401
    _compute_dep_graph,
    _compute_latency_histograms,
    _compute_session_timeline,
    _compute_tool_cooccurrence,
    _compute_tool_transitions,
    _compute_weekly_trends,
)
from reflect.insights import (  # noqa: F401
    _percentile,
    build_observations,
    build_practical_examples,
    build_recommendations,
    build_strengths,
    compute_session_quality,
    compute_token_economy,
    compute_tool_percentiles,
)
from reflect.models import AgentStats, TelemetryStats  # noqa: F401
from reflect.parsing import (  # noqa: F401
    _canonical_otlp_traces_path,
    _discover_rich_session_files,
    _extract_session_id,
    _flatten_otlp_attributes,
    _flatten_text_content,
    _infer_otlp_logs_file,
    _iter_claude_session_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_session_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)
from reflect.processing import _process_span, analyze_telemetry  # noqa: F401
from reflect.report import render_report  # noqa: F401
from reflect.terminal import _render_terminal  # noqa: F401
from reflect.utils import (  # noqa: F401
    _bar,
    _fmt_dur,
    _fmt_model,
    _fmt_tokens,
    _json_dumps,
    _json_loads,
    _safe_ratio,
    _stat_panel,
    logger,
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_otlp_traces() -> Path | None:
    """Return the canonical default OTLP traces path if it exists."""
    p_otlp = _canonical_otlp_traces_path()
    if p_otlp.exists():
        return p_otlp
    p = REFLECT_HOME / "state" / "otel-traces.json"
    return p if p.exists() else None


def _default_spans_dir() -> Path:
    """Return the default spans directory — prefer ~/.reflect, fallback to hook home."""
    p = REFLECT_HOME / "state" / "local_spans"
    if p.is_dir() or Path.home() / ".reflect" != REFLECT_HOME:
        return p
    return HOOK_HOME / ".state" / "local_spans"


def _default_sessions_dir() -> Path:
    p = REFLECT_HOME / "state" / "sessions"
    if p.is_dir() or Path.home() / ".reflect" != REFLECT_HOME:
        return p
    return HOOK_HOME / ".state" / "sessions"


def _default_vscode_copilot_dir() -> Path:
    system = platform.system()
    home = Path.home()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    if system == "Windows":
        return home / "AppData" / "Roaming" / "Code" / "User"
    return home / ".config" / "Code" / "User"


_AGENT_SPECS = [
    {
        "name": "Claude Code",
        "env": "CLAUDE_HOME",
        "default": lambda: Path.home() / ".claude",
        "path_kind": "home",
        "skill_path": ".claude/skills/",
        "global_path": "~/.claude/skills/",
        "recommendation": "Run reflect setup to wire Claude hooks and enable native Claude telemetry.",
    },
    {
        "name": "Cursor",
        "env": "CURSOR_HOME",
        "default": lambda: Path.home() / ".cursor",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.cursor/skills/",
        "recommendation": "Use session/log adapters for desktop; hooks help for headless or CLI launches. Treat state.vscdb as auth/context, not a guaranteed per-session token ledger.",
    },
    {
        "name": "Gemini CLI",
        "env": "GEMINI_HOME",
        "env_aliases": ["GEMINI_DIR"],
        "default": lambda: Path.home() / ".gemini",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.gemini/skills/",
        "recommendation": "Prefer native Gemini OTel; keep session/log adapters for troubleshooting.",
    },
    {
        "name": "GitHub Copilot",
        "env": "COPILOT_HOME",
        "default": lambda: Path.home() / ".copilot",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.copilot/skills/",
        "recommendation": "Prefer native Copilot OTel on OTLP HTTP; add hooks for governance.",
    },
    {
        "name": "OpenAI Codex CLI",
        "env": "CODEX_HOME",
        "default": lambda: Path.home() / ".codex",
        "path_kind": "home",
        "skill_path": ".codex/skills/",
        "global_path": "~/.codex/skills/",
        "recommendation": "Use native Codex OTel for interactive runs; reflect does not yet ship a native session adapter for Codex logs.",
    },
    {
        "name": "Windsurf",
        "env": "WINDSURF_HOME",
        "default": lambda: Path.home() / ".codeium" / "windsurf",
        "path_kind": "home",
        "skill_path": ".windsurf/skills/",
        "global_path": "~/.codeium/windsurf/skills/",
        "recommendation": "Native OTel and hooks still need verification for Windsurf.",
    },
    {
        "name": "Trae",
        "env": "TRAE_HOME",
        "default": lambda: Path.home() / ".trae",
        "path_kind": "home",
        "skill_path": ".trae/skills/",
        "global_path": "~/.trae/skills/",
        "recommendation": "Native OTel and hooks still need verification for Trae.",
    },
    {
        "name": "Cline",
        "env": "CLINE_HOME",
        "default": lambda: Path.home() / ".agents",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.agents/skills/",
        "recommendation": "Compatible with standard .agents/skills distribution.",
    },
    {
        "name": "Roo Code",
        "env": "ROO_HOME",
        "default": lambda: Path.home() / ".roo",
        "path_kind": "home",
        "skill_path": ".roo/skills/",
        "global_path": "~/.roo/skills/",
        "recommendation": "Native OTel and hooks still need verification for Roo Code.",
    },
    {
        "name": "Continue",
        "env": "CONTINUE_HOME",
        "default": lambda: Path.home() / ".continue",
        "path_kind": "home",
        "skill_path": ".continue/skills/",
        "global_path": "~/.continue/skills/",
        "recommendation": "Add hooks to cover exec / mcp-server gaps in Continue.",
    },
    {
        "name": "Goose",
        "env": "GOOSE_HOME",
        "default": lambda: Path.home() / ".config" / "goose",
        "path_kind": "home",
        "skill_path": ".goose/skills/",
        "global_path": "~/.config/goose/skills/",
        "recommendation": "Native OTel and hooks still need verification for Goose.",
    },
    {
        "name": "OpenHands",
        "env": "OPENHANDS_HOME",
        "default": lambda: Path.home() / ".openhands",
        "path_kind": "home",
        "skill_path": ".openhands/skills/",
        "global_path": "~/.openhands/skills/",
        "recommendation": "Native OTel and hooks still need verification for OpenHands.",
    },
    {
        "name": "Antigravity",
        "env": "ANTIGRAVITY_HOME",
        "default": lambda: Path.home() / ".gemini" / "antigravity",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.gemini/antigravity/skills/",
        "recommendation": "Core target for reflect telemetry and skill distribution.",
    },
    {
        "name": "Amp",
        "env": "AMP_HOME",
        "default": lambda: Path.home() / ".local" / "share" / "amp",
        "path_kind": "home",
        "skill_path": ".agents/skills/",
        "global_path": "~/.config/agents/skills/",
        "recommendation": "Start with session/log adapters before adding new default hook collection.",
    },
    {
        "name": "iFlow",
        "env": "IFLOW_HOME",
        "default": lambda: Path.home() / ".iflow",
        "path_kind": "home",
        "skill_path": ".iflow/skills/",
        "global_path": "~/.iflow/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "Pi",
        "env": "PI_HOME",
        "default": lambda: Path.home() / ".pi",
        "path_kind": "home",
        "skill_path": ".pi/skills/",
        "global_path": "~/.pi/agent/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "OpenClaw",
        "env": "OPENCLAW_HOME",
        "default": lambda: Path.home() / ".openclaw",
        "path_kind": "home",
        "skill_path": "skills/",
        "global_path": "~/.openclaw/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "OpenCode",
        "env": "OPENCODE_HOME",
        "default": lambda: Path.home() / ".config" / "opencode",
        "path_kind": "home",
        "skill_path": ".opencode/skills/",
        "global_path": "~/.config/opencode/skills/",
        "recommendation": "Use opencode run for skill extraction; opencode export for session telemetry.",
    },
]

_IMPLEMENTED_AGENT_SUPPORT: dict[str, tuple[str, str]] = {
    "Claude Code": ("Native OTel + hooks", "High"),
    "Cursor": ("Session/log adapters", "Medium"),
    "Gemini CLI": ("Native OTel + session adapters", "High"),
    "GitHub Copilot": ("Native OTel + VS Code env", "High"),
    "OpenAI Codex CLI": ("Native OTel config", "Medium"),
    "OpenCode": ("opencode run + export", "Medium"),
}


def _agent_support_summary(name: str) -> dict[str, str]:
    telemetry_path, confidence = _IMPLEMENTED_AGENT_SUPPORT.get(
        name,
        ("Not implemented yet (setup only snapshots skills/config)", "Planned"),
    )
    status = "Implemented" if name in _IMPLEMENTED_AGENT_SUPPORT else "Planned"
    return {
        "support_status": status,
        "telemetry_path": telemetry_path,
        "confidence": confidence,
    }


def _agent_path(spec: dict) -> Path:
    override = os.environ.get(spec["env"])
    if not override:
        for alias in spec.get("env_aliases", []):
            override = os.environ.get(alias)
            if override:
                break
    if override:
        return Path(override).expanduser()
    return spec["default"]().expanduser()


def _count_path_entries(path: Path, *, max_entries: int = 5000) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        for _ in path.rglob("*") if path.is_dir() else [path]:
            count += 1
            if count >= max_entries:
                return count
    except OSError:
        return 0
    return count


def _detect_agents() -> list[dict]:
    agents: list[dict] = []
    for spec in _AGENT_SPECS:
        path = _agent_path(spec)
        detected = path.exists()
        agents.append({
            **spec,
            **_agent_support_summary(spec["name"]),
            "path": path,
            "detected": detected,
            "entries": _count_path_entries(path) if detected else 0,
        })
    return agents


def _infer_default_otlp_logs() -> Path | None:
    return _infer_otlp_logs_file(_default_otlp_traces())


def _summarize_file(path: Path | None) -> str:
    if path is None or not path.exists():
        return "missing"
    try:
        size = path.stat().st_size
    except OSError:
        return "present"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _count_glob(path: Path, pattern: str) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    return sum(1 for _ in path.glob(pattern))


_UPDATE_CACHE_PATH = REFLECT_HOME / "state" / "update-check.json"
_UPDATE_CACHE_TTL_SECONDS = 60 * 60 * 12
_UPDATE_PYPI_JSON_URL = "https://pypi.org/pypi/o11y-reflect/json"


def _current_reflect_version() -> str:
    try:
        return importlib_metadata.version("o11y-reflect")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0"


def _version_key(version: str) -> tuple[object, ...]:
    parts: list[object] = []
    for token in re.findall(r"\d+|[A-Za-z]+", version or ""):
        parts.append(int(token) if token.isdigit() else token.lower())
    return tuple(parts)


def _is_newer_version(candidate: str | None, current: str) -> bool:
    if not candidate:
        return False
    return _version_key(candidate) > _version_key(current)


def _load_update_cache() -> dict:
    if not _UPDATE_CACHE_PATH.exists():
        return {}
    try:
        return _json_loads(_UPDATE_CACHE_PATH.read_text())
    except Exception as exc:
        logger.warning("Failed to read update cache %s: %s", _UPDATE_CACHE_PATH, exc)
        return {}


def _save_update_cache(payload: dict) -> None:
    _UPDATE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _UPDATE_CACHE_PATH.write_text(_json_stdlib.dumps(payload, indent=2) + "\n")


def _fetch_latest_reflect_version(timeout: float = 1.5) -> str | None:
    url = os.environ.get("REFLECT_UPDATE_PYPI_JSON_URL", _UPDATE_PYPI_JSON_URL)
    try:
        with urllib_request.urlopen(url, timeout=timeout) as response:
            payload = _json_loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib_error.URLError):
        return None
    info = payload.get("info") if isinstance(payload, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return version if isinstance(version, str) and version.strip() else None


def _release_update_status(*, allow_remote: bool) -> dict:
    current_version = _current_reflect_version()
    cache = _load_update_cache()
    latest_version = cache.get("latest_version") if isinstance(cache.get("latest_version"), str) else None
    checked_at = cache.get("checked_at") if isinstance(cache.get("checked_at"), str) else None
    source = "cache" if latest_version else "unknown"
    cache_fresh = False

    if checked_at:
        try:
            checked_dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            cache_fresh = (datetime.now(UTC) - checked_dt).total_seconds() < _UPDATE_CACHE_TTL_SECONDS
        except ValueError:
            checked_at = None

    if allow_remote and (not cache_fresh or not latest_version):
        fetched_version = _fetch_latest_reflect_version()
        if fetched_version:
            latest_version = fetched_version
            checked_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            source = "remote"
            _save_update_cache({
                "latest_version": latest_version,
                "checked_at": checked_at,
            })

    update_available = _is_newer_version(latest_version, current_version)
    return {
        "current_version": current_version,
        "latest_version": latest_version,
        "checked_at": checked_at,
        "update_available": update_available,
        "source": source,
    }


def _file_signature(path: Path) -> tuple[int, str] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        stat = path.stat()
        return stat.st_size, path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _directory_signature(path: Path) -> dict[str, tuple[int, str]]:
    signature: dict[str, tuple[int, str]] = {}
    if not path.exists() or not path.is_dir():
        return signature
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix()
        file_sig = _file_signature(child)
        if file_sig is not None:
            signature[rel] = file_sig
    return signature


def _find_repo_reflect_dir() -> Path | None:
    candidate = Path.cwd() / "reflect" / "src" / "reflect"
    return candidate if candidate.exists() else None


def _find_pipx_reflect_package_dir() -> Path | None:
    base = Path.home() / ".local" / "pipx" / "venvs" / "o11y-reflect" / "lib"
    if not base.exists():
        return None
    for python_dir in sorted(base.glob("python*/site-packages/reflect")):
        if python_dir.exists():
            return python_dir
    return None


def _detect_live_install_drift() -> dict | None:
    repo_reflect = _find_repo_reflect_dir()
    pipx_reflect = _find_pipx_reflect_package_dir()
    if repo_reflect is None or pipx_reflect is None:
        return None

    mismatches: list[str] = []
    for rel_path in ("dashboard.py", "insights.py", "data/index.html"):
        source_sig = _file_signature(repo_reflect / rel_path)
        live_sig = _file_signature(pipx_reflect / rel_path)
        if source_sig is None or live_sig is None:
            continue
        if source_sig != live_sig:
            mismatches.append(rel_path)

    if not mismatches:
        return None

    return {
        "component": "Live pipx install",
        "summary": f"{len(mismatches)} published file(s) differ from the repo checkout: {', '.join(mismatches)}.",
        "remediation": "Reinstall or re-sync the pipx package before validating the live dashboard.",
    }


def _bundled_reflect_skill_dir() -> Path | None:
    packaged = Path(__file__).parent / "data" / "skills" / "reflect"
    repo_copy = Path.cwd() / "reflect" / "skills" / "reflect"
    if repo_copy.exists():
        return repo_copy
    if packaged.exists():
        return packaged
    return None


def _default_publish_artifact_path() -> Path:
    docs_root = Path.cwd() / "reflect" / "docs"
    if not docs_root.exists():
        docs_root = Path.cwd() / "docs"
    return docs_root / "reports" / "latest.json"


def _publish_url_for_artifact(path: Path) -> str | None:
    report_ref = _artifact_report_ref(path)
    if not report_ref:
        return None
    for parent in [path.resolve().parent, *path.resolve().parents]:
        if parent.name == "docs":
            return f"{(parent / 'index.html').resolve().as_uri()}?report={report_ref}"
    return None


def _detect_skill_drift(agents: list[dict]) -> dict | None:
    source_dir = _bundled_reflect_skill_dir()
    if source_dir is None:
        return None

    source_signature = _directory_signature(source_dir)
    if not source_signature:
        return None

    drifted_targets: list[str] = []
    missing_targets: list[str] = []
    for agent in agents:
        if not agent.get("detected"):
            continue
        global_skill = Path(agent["global_path"]).expanduser() / "reflect"
        if not global_skill.exists():
            missing_targets.append(agent["name"])
            continue
        if _directory_signature(global_skill) != source_signature:
            drifted_targets.append(agent["name"])

    if not drifted_targets and not missing_targets:
        return None

    details: list[str] = []
    if drifted_targets:
        details.append(f"out of date for {', '.join(drifted_targets)}")
    if missing_targets:
        details.append(f"missing for {', '.join(missing_targets)}")

    return {
        "component": "Reflect skill copies",
        "summary": "Global skill distribution is " + "; ".join(details) + ".",
        "remediation": "Run reflect setup from the workspace root to refresh installed skill copies.",
    }


def _claude_hooks_registered() -> bool | None:
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return None
    try:
        settings = _json_loads(settings_path.read_text())
    except Exception:
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    for event in [*_HOOK_EVENTS, *_HOOK_EVENTS_WITH_MATCHER]:
        entries = hooks.get(event, [])
        if not any("otel-hook" in str(entry) for entry in entries):
            return False
    return True


def _detect_hook_drift() -> dict | None:
    if not shutil.which("otel-hook"):
        return None

    config_path = HOOK_HOME / "otel_config.json"
    issues: list[str] = []
    if not config_path.exists():
        issues.append("hook export config is missing")
    else:
        try:
            config = _json_loads(config_path.read_text())
        except Exception:
            issues.append("hook export config could not be read")
        else:
            if config.get("IDE_OTEL_LOCAL_SPANS") != "true":
                issues.append("IDE_OTEL_LOCAL_SPANS is not enabled")
            if not config.get(_HOOK_CFG_ENDPOINT_KEY):
                issues.append(f"{_HOOK_CFG_ENDPOINT_KEY} is missing from hook config")
            protocol = config.get(_HOOK_CFG_PROTOCOL_KEY)
            if not protocol:
                issues.append(f"{_HOOK_CFG_PROTOCOL_KEY} is missing from hook config")
            elif protocol not in {"grpc", "http/protobuf"}:
                issues.append(
                    f"{_HOOK_CFG_PROTOCOL_KEY} has unsupported value in hook config: {protocol}"
                )

    claude_status = _claude_hooks_registered()
    if claude_status is False:
        issues.append("Claude Code hooks are incomplete")

    if not issues:
        return None

    return {
        "component": "Hook wiring",
        "summary": "; ".join(issues) + ".",
        "remediation": "Run reflect setup to repair hook export config and Claude hook registration.",
    }


def _collect_update_advisor(*, allow_remote: bool) -> dict:
    agents = _detect_agents()
    local_issues = [
        issue
        for issue in (
            _detect_live_install_drift(),
            _detect_skill_drift(agents),
            _detect_hook_drift(),
        )
        if issue is not None
    ]
    return {
        "release": _release_update_status(allow_remote=allow_remote),
        "local_issues": local_issues,
    }


def _build_startup_update_notice(advisor: dict | None = None) -> str | None:
    advisor = advisor or _collect_update_advisor(allow_remote=False)
    release = advisor["release"]
    issues = [
        issue for issue in advisor["local_issues"]
        if issue["component"] != "Hook wiring"
    ]
    fragments: list[str] = []
    if release["update_available"]:
        fragments.append(
            f"v{release['latest_version']} is available (current: v{release['current_version']})"
        )
    if issues:
        fragments.append(f"{len(issues)} local drift issue(s) detected")
    if not fragments:
        return None
    return f"{'; '.join(fragments)}. Run reflect doctor for details."


def _render_update_advisor_panel(console, advisor: dict) -> None:
    from rich import box
    from rich.panel import Panel
    from rich.table import Table

    release = advisor["release"]
    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Component", style="bold cyan")
    table.add_column("Status", no_wrap=True)
    table.add_column("Details")
    table.add_column("Next step", overflow="fold")

    if release["update_available"]:
        release_status = "[yellow]update available[/]"
        release_details = (
            f"Installed v{release['current_version']}; latest release is v{release['latest_version']}."
        )
        release_next = "Run reflect update --apply to upgrade the pipx package."
    elif release["latest_version"]:
        release_status = "[green]current[/]"
        release_details = f"Installed v{release['current_version']} matches the latest known release."
        release_next = "No package action needed."
    else:
        release_status = "[dim]unknown[/]"
        release_details = f"Installed v{release['current_version']}; latest release could not be checked."
        release_next = "Re-run reflect doctor when network access is available."

    if release["checked_at"]:
        release_details += f" Last checked: {release['checked_at']}."
    table.add_row("Package release", release_status, release_details, release_next)

    for issue in advisor["local_issues"]:
        table.add_row(
            issue["component"],
            "[yellow]drift[/]",
            issue["summary"],
            issue["remediation"],
        )

    if not advisor["local_issues"]:
        table.add_row(
            "Local install state",
            "[green]healthy[/]",
            "No skill, hook, or live-install drift was detected.",
            "No local repair needed.",
        )

    console.print(Panel(table, title="Update advisor", border_style="cyan"))


def _resolve_and_analyze(
    *,
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    demo: bool,
    time_range: str,
) -> tuple[TelemetryStats, Path | None, Path, Path, str, datetime | None]:
    """Shared data-loading logic for main and subcommands."""
    if demo:
        _demo_traces = Path(__file__).parent / "data" / "demo-traces.json"
        if not _demo_traces.exists():
            _demo_traces = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
        if not _demo_traces.exists():
            click.echo("Demo data not found. Re-install the package or run from the repo root.", err=True)
            raise SystemExit(1)
        otlp_traces = _demo_traces
        sessions_dir = sessions_dir or Path(os.devnull)
        spans_dir = spans_dir or Path(os.devnull)
        time_range = "all"

    since: datetime | None = None
    if time_range != "all":
        from datetime import timedelta
        now = datetime.now(tz=UTC)
        deltas = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
        since = now - deltas[time_range]

    if sessions_dir is None:
        sessions_dir = _default_sessions_dir()
    if spans_dir is None:
        spans_dir = _default_spans_dir()
    if otlp_traces is None:
        otlp_traces = _default_otlp_traces()

    stats = analyze_telemetry(sessions_dir, spans_dir, otlp_traces, since=since)
    return stats, otlp_traces, sessions_dir, spans_dir, time_range, since


@click.group(invoke_without_command=True)
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing session metadata JSON files.",
)
@click.option(
    "--spans-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing local span JSONL files.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Output markdown file path. Defaults to ~/.reflect/reports/ai-usage-telemetry-report-<date>.md.",
)
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file from the collector file exporter.",
)
@click.option(
    "--terminal/--no-terminal",
    default=True,
    help="Render an interactive dashboard in the terminal using rich (default). Use --no-terminal to save a markdown report instead.",
)
@click.option(
    "--dashboard-artifact",
    type=click.Path(path_type=Path),
    default=None,
    help="Write the dashboard JSON artifact to a file. Best when serving docs/index.html locally or from GitHub Pages.",
)
@click.option(
    "--demo",
    is_flag=True,
    help="Run with bundled sample data. Great for first-time users or screenshots.",
)
@click.option("--day", "time_range", flag_value="day", help="Analyze last 24 hours.")
@click.option("--week", "time_range", flag_value="week", default=True, help="Analyze last 7 days (default).")
@click.option("--month", "time_range", flag_value="month", help="Analyze last 30 days.")
@click.option("--all", "time_range", flag_value="all", help="Analyze all available data.")
@click.pass_context
def main(
    ctx: click.Context,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    output: Path | None,
    otlp_traces: Path | None,
    terminal: bool,
    dashboard_artifact: Path | None,
    demo: bool,
    time_range: str,
) -> None:
    """AI usage telemetry report — analyze OpenTelemetry span data from your AI sessions."""
    if ctx.invoked_subcommand is not None:
        return

    stats, otlp_traces, sessions_dir, spans_dir, time_range, since = _resolve_and_analyze(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        demo=demo,
        time_range=time_range,
    )

    # Resolve output path (main-specific)
    if output is None:
        out_dir = REFLECT_HOME / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"ai-usage-telemetry-report-{datetime.now().strftime('%Y-%m-%d')}.md"

    update_notice = _build_startup_update_notice()
    if update_notice:
        click.echo(f"reflect notice: {update_notice}")
    if dashboard_artifact is not None:
        _write_dashboard_artifact(stats, dashboard_artifact)

    if terminal:
        _render_terminal(
            stats,
            publish_url=None,
            time_range=time_range,
            since=since,
        )

    if not terminal:
        render_report(stats, sessions_dir, spans_dir, output)

        print(f"Report saved to:   {output}")
        if dashboard_artifact is not None:
            print(f"Dashboard JSON:    {dashboard_artifact}")
        print(f"Analyzed events:   {stats.total_events:,}")
        print(f"Sessions:          {len(stats.sessions_seen)} unique")
        print(f"Active days:       {stats.days_active}")
        print(f"Top model:         {stats.models_by_count.most_common(1)[0][0] if stats.models_by_count else 'N/A'}")
        print(f"Tool-to-prompt:    {_safe_ratio(stats.events_by_type.get('PreToolUse', 0), stats.events_by_type.get('UserPromptSubmit', 0)):.1f}:1")


# ---------------------------------------------------------------------------
# Report command
# ---------------------------------------------------------------------------


@main.command()
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file from the collector file exporter.",
)
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing session metadata JSON files.",
)
@click.option(
    "--spans-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing local span JSONL files.",
)
@click.option("--day", "time_range", flag_value="day", help="Analyze last 24 hours.")
@click.option("--week", "time_range", flag_value="week", default=True, help="Analyze last 7 days (default).")
@click.option("--month", "time_range", flag_value="month", help="Analyze last 30 days.")
@click.option("--all", "time_range", flag_value="all", help="Analyze all available data.")
@click.option(
    "--demo",
    is_flag=True,
    help="Run with bundled sample data.",
)
@click.option(
    "--dashboard-artifact",
    type=click.Path(path_type=Path),
    default=None,
    help="Also write the dashboard JSON artifact to a file.",
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    default=None,
    help="Also save a markdown report to this file.",
)
def report(
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    dashboard_artifact: Path | None,
    output: Path | None,
) -> None:
    """Open the AI usage dashboard in a browser via a local server."""
    stats, _, sessions_dir, spans_dir, _, _ = _resolve_and_analyze(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        demo=demo,
        time_range=time_range,
    )
    if dashboard_artifact is not None:
        _write_dashboard_artifact(stats, dashboard_artifact)
    if output is not None:
        render_report(stats, sessions_dir, spans_dir, output)
        print(f"Report saved to: {output}")
    _start_publish_server(stats)


# ---------------------------------------------------------------------------
# Skills command
# ---------------------------------------------------------------------------

# Known agent CLIs with their non-interactive (print-mode) flags.
# First entry in the list is the auto-detection priority order.
_SKILL_AGENT_SPECS: list[tuple[str, list[str]]] = [
    ("claude", ["--print"]),
    ("gemini", ["-p"]),
    ("codex", ["--print"]),
    ("cursor-agent", ["--print"]),
    ("copilot", ["--prompt"]),
    ("opencode", ["run"]),
    ("qwen", ["--print"]),
]
_SKILL_AGENT_NAMES = ", ".join(name for name, _ in _SKILL_AGENT_SPECS)


def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences wrapping JSON output, if present."""
    stripped = text.strip()
    match = re.search(r"```(?:json)?\s*\n(.*?)\n```", stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _compress_tool_sequence(tools: list[str]) -> list[str]:
    """Collapse consecutive identical tool calls into Tool×N notation."""
    if not tools:
        return []
    result: list[str] = []
    cur, count = tools[0], 1
    for t in tools[1:]:
        if t == cur:
            count += 1
        else:
            result.append(f"{cur}×{count}" if count > 1 else cur)
            cur, count = t, 1
    result.append(f"{cur}×{count}" if count > 1 else cur)
    return result


_ACTIONABLE_RECOVERY_EVENTS = {"PreToolUse", "BeforeShellExecution", "BeforeMCPExecution"}


def _extract_recovery_chains(spans: list[dict]) -> list[str]:
    """Return failed-tool→next-actionable-tool pairs as error-recovery signals.

    Spans are sorted chronologically by ``t`` first so ingestion order does not
    produce misleading pairs.  Only actionable span types (PreToolUse /
    BeforeShellExecution / BeforeMCPExecution) are considered as recovery steps,
    skipping Stop/SessionEnd and other bookkeeping events.
    """
    chains: list[str] = []
    ordered = sorted(
        spans,
        key=lambda s: (0, s["t"]) if s.get("t") is not None else (1, 0),
    )
    for i, span in enumerate(ordered):
        if span.get("ok", True):
            continue
        failed = span.get("tool")
        if not failed:
            continue
        for next_span in ordered[i + 1:]:
            if next_span.get("event") not in _ACTIONABLE_RECOVERY_EVENTS:
                continue
            recovered = next_span.get("tool")
            if recovered:
                chains.append(f"{failed}✗→{recovered}")
            break
    return chains


def _interactive_pick(
    items: list[str],
    *,
    multi: bool,
) -> list[int]:
    """Raw-terminal interactive picker. Returns list of selected indices.

    *multi=True*: space to toggle checkboxes, all start checked.
    *multi=False*: radio — arrows move cursor, Enter confirms.

    Falls back to a Click prompt when stdin is not a TTY or when the platform
    does not support raw-terminal mode (e.g. Windows without ``tty``/``termios``).
    """
    import sys

    n = len(items)
    if not sys.stdin.isatty():
        return list(range(n)) if multi else [0]

    try:
        import termios
        import tty
    except ImportError:
        # Platform (e.g. Windows) does not support raw-terminal mode.
        hint = "↑↓ select  Enter confirm" if not multi else "comma-separated numbers, empty=all"
        click.echo(hint)
        for i, label in enumerate(items, start=1):
            click.echo(f"  {i}. {label}")
        if multi:
            raw = click.prompt("Select by number (empty for all)", default="", show_default=False)
            if not raw.strip():
                return list(range(n))
            picked = []
            for part in raw.split(","):
                part = part.strip()
                if part.isdigit():
                    idx = int(part) - 1
                    if 0 <= idx < n:
                        picked.append(idx)
            return sorted(set(picked)) or list(range(n))
        choice = click.prompt("Select by number", type=click.IntRange(1, n), default=1)
        return [choice - 1]

    from rich.console import Console as _Console

    selected = [True] * n if multi else [False] * n
    if not multi:
        selected[0] = True
    cursor = 0

    hint = (
        "[dim]↑↓ move  Space toggle  a=all  n=none  Enter confirm[/dim]"
        if multi
        else "[dim]↑↓ move  Enter confirm[/dim]"
    )

    _in_raw_mode = False

    def _render() -> int:
        """Render the list and return the number of lines printed."""
        buf = io.StringIO()
        buf_con = _Console(file=buf, force_terminal=True, highlight=False)
        buf_con.print(hint)
        lines = 1
        buf_con.print()
        lines += 1
        for i, label in enumerate(items):
            arrow = "▶ " if i == cursor else "  "
            mark = "[green]●[/green]" if selected[i] else "[dim]○[/dim]"
            if i == cursor:
                buf_con.print(f"  {arrow}{mark} [bold]{label}[/bold]")
            else:
                buf_con.print(f"  {arrow}{mark} {label}")
            lines += 1
        output = buf.getvalue()
        if _in_raw_mode:
            # In raw mode \n only moves down — no carriage return — causing a
            # staircase. Replace with \r\n so each line starts at column 0.
            output = output.replace("\n", "\r\n")
        sys.stdout.write(output)
        sys.stdout.flush()
        return lines

    lines_drawn = _render()

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        _in_raw_mode = True
        while True:
            sys.stdout.write(f"\033[{lines_drawn}A\033[J")
            sys.stdout.flush()
            lines_drawn = _render()
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                break
            elif ch == " " and multi:
                selected[cursor] = not selected[cursor]
            elif ch == "a" and multi:
                selected = [True] * n
            elif ch == "n" and multi:
                selected = [False] * n
            elif ch == "\x1b":
                seq = sys.stdin.read(2)
                if seq == "[A":  # up arrow
                    cursor = (cursor - 1) % n
                    if not multi:
                        selected = [False] * n
                        selected[cursor] = True
                elif seq == "[B":  # down arrow
                    cursor = (cursor + 1) % n
                    if not multi:
                        selected = [False] * n
                        selected[cursor] = True
            elif ch == "\x03":
                raise KeyboardInterrupt
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return [i for i, s in enumerate(selected) if s]


def _resolve_skills_agent(agent: str | None) -> tuple[str, list[str]]:
    """Return (binary, extra_flags) for the chosen or auto-detected agent CLI.

    When ``--agent`` is given the named binary is used directly.  Otherwise
    all supported CLIs are probed and, if more than one is found, the user is
    prompted to pick one interactively (arrows + Enter).
    """
    if agent is not None:
        for name, flags in _SKILL_AGENT_SPECS:
            if name == agent:
                return agent, flags
        # Unknown agent — fall back to --print and hope for the best
        return agent, ["--print"]

    # Probe all supported CLIs
    available = [(name, flags) for name, flags in _SKILL_AGENT_SPECS if shutil.which(name)]

    if not available:
        click.echo(
            f"No supported agent CLI found (tried: {_SKILL_AGENT_NAMES}).\n"
            "Install one or pass --agent <binary>.",
            err=True,
        )
        raise SystemExit(1)

    if len(available) == 1:
        return available[0]

    # Multiple agents available — let the user choose
    from rich.console import Console as _Console
    _con = _Console(force_terminal=True)
    _con.print(f"\n[bold]Found {len(available)} agent CLIs.[/bold] Which should extract skills?\n")
    labels = [name for name, _ in available]
    indices = _interactive_pick(labels, multi=False)
    chosen = indices[0] if indices else 0
    return available[chosen]


def _select_skills(
    skill_defs: list[dict],
    console: object,
    *,
    yes: bool,
) -> list[dict]:
    """Let the user pick which extracted skills to install.

    In an interactive terminal: renders a space-to-toggle checkbox list
    (↑↓ navigate, Space toggle, a=all, n=none, Enter confirm).

    In non-interactive mode (piped / --yes): returns all skills or prompts
    for a comma-separated index list as a fallback.
    """
    import sys

    console.print(f"\nExtracted [bold]{len(skill_defs)}[/bold] skill(s):\n")

    if yes:
        for s in skill_defs:
            console.print(f"  [green]✓[/green] [cyan]{s['name']:<22}[/cyan] {s['description']}")
        return skill_defs

    if not sys.stdin.isatty():
        # Non-interactive fallback: numbered list + comma prompt
        for i, s in enumerate(skill_defs, 1):
            console.print(f"  [bold]{i}.[/bold] [cyan]{s['name']:<22}[/cyan] {s['description']}")
        console.print()
        raw = click.prompt(
            "Select skills to install (e.g. 1,3) or press Enter for all",
            default="all",
            show_default=True,
        ).strip()
        if raw.lower() in ("all", ""):
            return skill_defs
        chosen: list[dict] = []
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                idx = int(token)
                if 1 <= idx <= len(skill_defs):
                    chosen.append(skill_defs[idx - 1])
                else:
                    console.print(f"  [yellow]Skipping out-of-range index {idx}[/yellow]")
            except ValueError:
                console.print(f"  [yellow]Ignoring non-numeric token '{token}'[/yellow]")
        if not chosen:
            console.print("No valid skills selected. Aborted.")
            raise SystemExit(0)
        return chosen

    # Interactive terminal: space-toggleable checkboxes
    labels = [
        f"[cyan]{s['name']:<22}[/cyan] {s['description']}"
        for s in skill_defs
    ]
    indices = _interactive_pick(labels, multi=True)
    if not indices:
        console.print("\n[yellow]No skills selected. Aborted.[/yellow]")
        raise SystemExit(0)
    return [skill_defs[i] for i in indices]


def _serialize_sessions_for_skills(stats: TelemetryStats) -> str:
    """Serialize top sessions to compact workflow fingerprints for skill extraction.

    Each session is encoded as structured trace-derived signals rather than raw
    conversation text: ordered tool flows, shell commands, prompt topic snippets,
    and error-recovery chains.  This gives the extraction AI real behavioral
    patterns to reason about at ~200 tokens/session instead of tens of thousands.
    """
    session_ids = sorted(
        stats.sessions_seen,
        key=lambda sid: stats.session_events.get(sid, 0),
        reverse=True,
    )[:20]

    lines: list[str] = []
    for sid in session_ids:
        event_count = stats.session_events.get(sid, 0)
        models = stats.session_models.get(sid)
        model_str = next(iter(models), "unknown") if models else "unknown"
        tok = stats.session_tokens.get(sid, {})
        total_tokens = tok.get("input", 0) + tok.get("output", 0)

        lines.append(f"Session {sid[:8]}:")
        lines.append(f"  model={model_str} events={event_count} tokens={total_tokens}")

        # Ordered tool flow — sorted by timestamp, consecutive repeats collapsed,
        # capped at 20 steps.  E.g. "Read×3 → Grep → Edit → Bash".
        tool_seq = stats.session_tool_seq.get(sid, [])
        if tool_seq:
            sorted_seq = sorted(tool_seq, key=lambda item: item[0])
            compressed = _compress_tool_sequence([t for _, t, _ in sorted_seq])
            lines.append(f"  tool_flow={' → '.join(compressed[:20])}")

        # Top shell commands reveal domain (git, pytest, docker, npm …).
        shell_cmds = stats.session_shell_commands.get(sid)
        if shell_cmds:
            top_cmds = [cmd for cmd, _ in shell_cmds.most_common(5)]
            lines.append(f"  shell_cmds={' | '.join(top_cmds)}")

        # First 80 chars of each user prompt normalized to a single line.
        # Raw previews may contain newlines that would break the line-oriented format.
        conv = stats.session_conversation.get(sid, [])
        prompt_snippets = []
        for e in conv:
            if e.get("type") == "prompt" and e.get("preview"):
                snippet = " ".join(e["preview"].split())[:80]
                if snippet:
                    prompt_snippets.append(snippet)
        if prompt_snippets:
            joined = " / ".join(prompt_snippets[:3])
            lines.append(f"  prompts=[{joined}]")

        # Error-recovery chains: failed tool → next tool shows debugging patterns.
        spans = stats.session_span_details.get(sid, [])
        recoveries = _extract_recovery_chains(spans)
        if recoveries:
            lines.append(f"  error_recovery={' | '.join(recoveries[:3])}")

    return "\n".join(lines)


# Strict kebab-case: lowercase letters, digits, and hyphens only; 1-64 chars.
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")


def _validate_skill_name(name: object) -> str:
    """Return *name* if it is a safe path component, otherwise raise ``ValueError``."""
    if not isinstance(name, str):
        raise ValueError(f"Skill name must be a string, got {type(name).__name__!r}")
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(
            f"Skill name {name!r} is not a valid kebab-case identifier "
            "(use lowercase letters, digits, and hyphens only; 1-64 chars)"
        )
    return name


@main.command()
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file from the collector file exporter.",
)
@click.option(
    "--sessions-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing session metadata JSON files.",
)
@click.option(
    "--spans-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory containing local span JSONL files.",
)
@click.option("--day", "time_range", flag_value="day", help="Analyze last 24 hours.")
@click.option("--week", "time_range", flag_value="week", default=True, help="Analyze last 7 days (default).")
@click.option("--month", "time_range", flag_value="month", help="Analyze last 30 days.")
@click.option("--all", "time_range", flag_value="all", help="Analyze all available data.")
@click.option(
    "--demo",
    is_flag=True,
    help="Run with bundled sample data.",
)
@click.option(
    "--agent",
    default=None,
    help=(
        "Agent CLI binary to use for skill extraction "
        "(e.g. claude, gemini, codex). Auto-detected if not set."
    ),
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    help="Install all extracted skills without prompting for selection.",
)
def skills(
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    agent: str | None,
    yes: bool,
) -> None:
    """Extract reusable skills from your AI sessions using an agent."""
    import json as _json
    import subprocess
    import tempfile

    from rich.console import Console
    console = Console(force_terminal=True)

    agent_bin, agent_flags = _resolve_skills_agent(agent)

    stats, _, _, _, _, _ = _resolve_and_analyze(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        demo=demo,
        time_range=time_range,
    )

    session_summaries = _serialize_sessions_for_skills(stats)
    try:
        prompt_pkg = importlib_resources.files("reflect") / "data" / "skills-extraction-prompt.md"
        prompt_text = prompt_pkg.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError) as exc:
        click.echo(
            f"Could not load skills extraction prompt: {exc}",
            err=True,
        )
        raise SystemExit(1) from exc
    prompt = prompt_text + "\n" + session_summaries

    with console.status(
        f"[bold]Extracting skills with {agent_bin}...[/bold]",
        spinner="dots",
    ):
        result = subprocess.run([agent_bin, *agent_flags, prompt], capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(f"Agent exited with code {result.returncode}:\n{result.stderr}", err=True)
        raise SystemExit(1)

    raw_output = _strip_json_fences(result.stdout)
    try:
        skill_defs = _json.loads(raw_output)
    except _json.JSONDecodeError as exc:
        click.echo(
            f"Could not parse agent output as JSON: {exc}\n\nOutput:\n{result.stdout[:500]}",
            err=True,
        )
        raise SystemExit(1) from exc

    selected = _select_skills(skill_defs, console, yes=yes)

    detected = [a for a in _detect_agents() if a["detected"]]
    if not yes:
        console.print()
        confirmed = click.confirm(
            f"Write {len(selected)} skill(s) to {len(detected)} detected agent(s)?",
            default=True,
        )
        if not confirmed:
            console.print("Aborted.")
            return

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for s in selected:
            try:
                safe_name = _validate_skill_name(s.get("name"))
            except ValueError as exc:
                click.echo(f"Skipping invalid skill name: {exc}", err=True)
                continue
            skill_dir = tmp_path / safe_name
            skill_dir.mkdir()
            skill_md = (
                f"---\nname: {safe_name}\ndescription: {s['description']}\n---\n\n{s['content']}"
            )
            (skill_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")

        console.print()
        for agent_spec in detected:
            global_path = Path(agent_spec["global_path"]).expanduser()
            global_path.mkdir(parents=True, exist_ok=True)
            for s in selected:
                try:
                    safe_name = _validate_skill_name(s.get("name"))
                except ValueError:
                    continue  # already warned above
                src = tmp_path / safe_name
                if not src.exists():
                    continue
                dest = global_path / safe_name
                # Ensure dest stays within the intended skills directory (must be a subdirectory)
                resolved_dest = dest.resolve()
                resolved_base = global_path.resolve()
                if not str(resolved_dest).startswith(str(resolved_base) + os.sep):
                    click.echo(f"Skipping skill {safe_name!r}: resolved path escapes skills dir", err=True)
                    continue
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src, dest)
            console.print(f"  [green]✓[/green] {agent_spec['name']}: {global_path}")

    names = ", ".join(f"/{s['name']}" for s in selected)
    console.print(
        f"\n[bold green]{len(selected)} skill(s) ready.[/bold green] Use {names} in Claude Code."
    )


# ---------------------------------------------------------------------------
# Setup command
# ---------------------------------------------------------------------------

_HOOK_EVENTS = [
    "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentStop",
    "UserPromptSubmit", "Stop",
]
_HOOK_EVENTS_WITH_MATCHER = [
    "PreToolUse", "PostToolUse", "PostToolUseFailure",
]


# Pin to a specific release tag so skill distribution is reproducible.
# Update this when a new opentelemetry-skill release should be adopted.
_OTEL_SKILL_REF = "main"  # TODO: pin to a release tag once the project cuts one
_OTEL_SKILL_ZIP = f"https://github.com/o11y-dev/opentelemetry-skill/archive/refs/heads/{_OTEL_SKILL_REF}.zip"


def _fetch_opentelemetry_skill(console) -> Path | None:
    """Download opentelemetry-skill from GitHub and cache it. Returns the skill dir or None."""
    # Derive from current REFLECT_HOME at call time so test patches take effect.
    skill_dir = REFLECT_HOME / "cache" / "opentelemetry-skill" / _OTEL_SKILL_REF
    if (skill_dir / "SKILL.md").exists():
        console.print(f"  [green]\u2713[/] opentelemetry-skill already cached ({skill_dir})")
        return skill_dir

    console.print("  [yellow]\u2022[/] Fetching opentelemetry-skill from GitHub...")
    try:
        with urllib_request.urlopen(_OTEL_SKILL_ZIP, timeout=30) as resp:
            data = resp.read()
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # The zip contains a single top-level dir like "opentelemetry-skill-main/"
            top = next(n for n in zf.namelist() if n.endswith("/") and n.count("/") == 1)
            if skill_dir.exists():
                shutil.rmtree(skill_dir)
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_root = skill_dir.resolve()
            for member in zf.namelist():
                if member == top or not member.startswith(top):
                    continue
                rel = member[len(top):]
                dest = skill_dir / rel
                resolved_dest = dest.resolve()
                if os.path.commonpath([str(skill_root), str(resolved_dest)]) != str(skill_root):
                    raise ValueError(f"Unsafe path in opentelemetry-skill archive: {member}")
                if member.endswith("/"):
                    resolved_dest.mkdir(parents=True, exist_ok=True)
                else:
                    resolved_dest.parent.mkdir(parents=True, exist_ok=True)
                    resolved_dest.write_bytes(zf.read(member))
        if (skill_dir / "SKILL.md").exists():
            console.print(f"  [green]\u2713[/] Fetched opentelemetry-skill \u2192 {skill_dir}")
            return skill_dir
        console.print("  [red]\u2717[/] opentelemetry-skill fetched but SKILL.md not found")
        return None
    except Exception as exc:
        console.print(f"  [red]\u2717[/] Failed to fetch opentelemetry-skill: {exc}")
        return None


def _distribute_skills(console) -> None:
    """Distribute reflect, skills, and opentelemetry-skill to detected agents."""
    # reflect and skills skills are bundled with the package
    bundled_skills_dir = Path(__file__).parent / "data" / "skills"

    available_skills: dict[str, Path] = {}

    reflect_skill = bundled_skills_dir / "reflect"
    if (reflect_skill / "SKILL.md").exists():
        available_skills["reflect"] = reflect_skill

    skills_skill = bundled_skills_dir / "skills"
    if (skills_skill / "SKILL.md").exists():
        available_skills["skills"] = skills_skill

    otel_skill = _fetch_opentelemetry_skill(console)
    if otel_skill:
        available_skills["opentelemetry-skill"] = otel_skill

    if not available_skills:
        console.print("  [yellow]\u2022[/] No skills available to distribute.")
        return

    # Filter detected agents
    detected_agents = [a for a in _detect_agents() if a.get("detected")]

    for agent in detected_agents:
        # 1. Global path (expanded from ~/...)
        try:
            global_skill_path = Path(agent["global_path"]).expanduser()
            global_skill_path.mkdir(parents=True, exist_ok=True)
            for skill_name, skill_src in available_skills.items():
                dest = global_skill_path / skill_name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_src, dest)
            console.print(f"  [green]\u2713[/] Distributed skills to [bold]{agent['name']}[/] global path")
        except Exception as e:
            console.print(f"  [red]\u2717[/] Failed to distribute to {agent['name']} global: {e}")

        # 2. Project path (local to workspace)
        try:
            project_skill_base = Path.cwd() / agent["skill_path"]
            project_skill_base.mkdir(parents=True, exist_ok=True)
            for skill_name, skill_src in available_skills.items():
                dest = project_skill_base / skill_name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_src, dest)
            console.print(f"  [green]\u2713[/] Distributed skills to [bold]{agent['name']}[/] project path")
        except Exception as e:
            console.print(f"  [red]\u2717[/] Failed to distribute to {agent['name']} project: {e}")


def _agent_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _reflect_agent_dir(agent_name: str) -> Path:
    return REFLECT_HOME / "agents" / _agent_slug(agent_name)


def _copy_config_snapshot(agent_name: str, source: Path) -> Path:
    dest_dir = _reflect_agent_dir(agent_name) / "config-snapshots"
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{source.stem}-{timestamp}{source.suffix}"
    shutil.copy2(source, dest)
    return dest


def _agent_config_paths(agent: dict) -> list[Path]:
    """Return every config path reflect knows to inspect for an agent."""
    home = Path.home()
    name = agent["name"]
    candidates: list[Path] = []
    if name == "Claude Code":
        candidates.append(home / ".claude" / "settings.json")
    elif name == "Gemini CLI":
        candidates.append(home / ".gemini" / "settings.json")
    elif name == "GitHub Copilot":
        candidates.extend([
            home / "Library" / "Application Support" / "Code" / "User" / "settings.json",
            home / "Library" / "Application Support" / "Code - Insiders" / "User" / "settings.json",
            home / ".config" / "Code" / "User" / "settings.json",
            home / ".config" / "Code - Insiders" / "User" / "settings.json",
        ])
    elif name == "OpenAI Codex CLI":
        candidates.append(home / ".codex" / "config.toml")
    elif name == "Qwen Code":
        candidates.append(home / ".qwen" / "settings.json")
    return candidates


def _agent_config_candidates(agent: dict) -> list[Path]:
    return [path for path in _agent_config_paths(agent) if path.exists()]


def _hook_otlp_endpoint(hook_config: dict[str, str]) -> str:
    return hook_config.get(_HOOK_CFG_ENDPOINT_KEY, _HOOK_CFG_ENDPOINT_DEFAULT)


def _hook_otlp_protocol(hook_config: dict[str, str]) -> str:
    return hook_config.get(_HOOK_CFG_PROTOCOL_KEY, _HOOK_CFG_PROTOCOL_DEFAULT)


def _copilot_otlp_endpoint(endpoint: str) -> str:
    return endpoint.replace(":4317", ":4318") if endpoint.endswith(":4317") else endpoint


def _gemini_otlp_protocol(protocol: str) -> str:
    return "http" if protocol.startswith("http") else "grpc"


def _native_otel_target(hook_config: dict[str, str], agent_name: str) -> dict[str, object]:
    grpc_endpoint = _hook_otlp_endpoint(hook_config)
    hook_protocol = _hook_otlp_protocol(hook_config)
    targets = {
        "Claude Code": {
            "endpoint": grpc_endpoint,
            "protocol": hook_protocol,
            "emit_logs": True,
            "emit_traces": False,
            "prompt_capture": False,
        },
        "GitHub Copilot": {
            "endpoint": _copilot_otlp_endpoint(grpc_endpoint),
            "protocol": "otlp-http",
            "emit_logs": True,
            "emit_traces": True,
            "prompt_capture": False,
        },
        "GitHub Copilot CLI": {
            "endpoint": _copilot_otlp_endpoint(grpc_endpoint),
            "protocol": "otlp-http",
            "emit_logs": True,
            "emit_traces": True,
            "prompt_capture": False,
        },
        "Gemini CLI": {
            "endpoint": grpc_endpoint,
            "protocol": _gemini_otlp_protocol(hook_protocol),
            "emit_logs": True,
            "emit_traces": True,
            "prompt_capture": False,
        },
        "OpenAI Codex CLI": {
            "endpoint": grpc_endpoint,
            "protocol": hook_protocol,
            "emit_logs": True,
            "emit_traces": True,
            "prompt_capture": False,
        },
    }
    return targets[agent_name]


def _claude_native_otel_env(hook_config: dict[str, str]) -> dict[str, object]:
    target = _native_otel_target(hook_config, "Claude Code")
    return {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": target["protocol"],
        "OTEL_EXPORTER_OTLP_ENDPOINT": target["endpoint"],
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
    }


def _copilot_native_otel_settings(hook_config: dict[str, str]) -> dict[str, object]:
    target = _native_otel_target(hook_config, "GitHub Copilot")
    return {
        "github.copilot.chat.otel.enabled": True,
        "github.copilot.chat.otel.otlpEndpoint": target["endpoint"],
        "github.copilot.chat.otel.exporterType": target["protocol"],
        "github.copilot.chat.otel.captureContent": target["prompt_capture"],
    }


def _copilot_cli_native_otel_env(hook_config: dict[str, str]) -> dict[str, object]:
    target = _native_otel_target(hook_config, "GitHub Copilot CLI")
    return {
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_OTLP_ENDPOINT": target["endpoint"],
    }


def _gemini_native_otel_settings(hook_config: dict[str, str]) -> dict[str, object]:
    target = _native_otel_target(hook_config, "Gemini CLI")
    return {
        "enabled": True,
        "target": "local",
        "useCollector": True,
        "otlpEndpoint": target["endpoint"],
        "otlpProtocol": target["protocol"],
        "logPrompts": target["prompt_capture"],
    }


def _codex_native_otel_settings(hook_config: dict[str, str]) -> dict[str, object]:
    target = _native_otel_target(hook_config, "OpenAI Codex CLI")
    endpoint = target["endpoint"]
    return {
        "exporter": {"otlp-grpc": {"endpoint": endpoint}},
        "traces_exporter": "otlp",
        "traces_endpoint": endpoint,
        "logs_exporter": "otlp",
        "logs_endpoint": endpoint,
        "log_user_prompt": target["prompt_capture"],
    }


def _codex_native_otel_matches_desired(otel: object, desired: dict[str, object]) -> bool:
    if not isinstance(otel, dict):
        return False
    return all(otel.get(key) == value for key, value in desired.items())


def _render_codex_native_otel_block(hook_config: dict[str, str]) -> str:
    desired = _codex_native_otel_settings(hook_config)
    endpoint = desired["traces_endpoint"]
    return (
        "[otel]\n"
        f'exporter = {{otlp-grpc = {{endpoint = "{endpoint}"}}}}\n'
        'traces_exporter = "otlp"\n'
        f'traces_endpoint = "{endpoint}"\n'
        'logs_exporter = "otlp"\n'
        f'logs_endpoint = "{endpoint}"\n'
        "log_user_prompt = false\n"
    )


def _upsert_toml_section(original: str, section_name: str, block: str) -> str:
    normalized_block = block.strip() + "\n"
    pattern = rf"^\[{re.escape(section_name)}\]\n.*?(?=^\[|\Z)"
    flags = re.MULTILINE | re.DOTALL
    if re.search(pattern, original, flags=flags):
        updated = re.sub(pattern, block.strip() + "\n\n", original, flags=flags)
        return re.sub(r"\n{3,}", "\n\n", updated).rstrip() + "\n"
    if not original.strip():
        return normalized_block
    return original.rstrip() + "\n\n" + normalized_block


def _missing_desired_keys(actual: object, desired: dict[str, object]) -> list[str]:
    if not isinstance(actual, dict):
        return list(desired)
    return [key for key, value in desired.items() if actual.get(key) != value]


def _collect_native_otel_statuses(hook_config: dict[str, str]) -> list[dict[str, str]]:
    statuses: list[dict[str, str]] = []

    claude_path = Path.home() / ".claude" / "settings.json"
    claude_desired = _claude_native_otel_env(hook_config)
    if not claude_path.exists():
        statuses.append({
            "agent": "Claude Code",
            "status": "missing",
            "details": "No settings.json found yet.",
            "path": str(claude_path),
        })
    else:
        try:
            claude_settings = _json_loads(claude_path.read_text())
        except Exception as exc:
            statuses.append({
                "agent": "Claude Code",
                "status": "unreadable",
                "details": f"Failed to read settings.json: {exc}",
                "path": str(claude_path),
            })
        else:
            issues = _missing_desired_keys(claude_settings.get("env"), claude_desired)
            statuses.append({
                "agent": "Claude Code",
                "status": "ready" if not issues else "incomplete",
                "details": (
                    "Native metrics/log export is configured; traces still rely on hooks or local session stores."
                    if not issues
                    else "Missing or incorrect env keys: " + ", ".join(issues)
                ),
                "path": str(claude_path),
            })

    copilot_paths = _agent_config_paths({"name": "GitHub Copilot"})
    existing_copilot_paths = [path for path in copilot_paths if path.exists()]
    copilot_desired = _copilot_native_otel_settings(hook_config)
    copilot_cli_desired = _copilot_cli_native_otel_env(hook_config)
    if not existing_copilot_paths:
        searched = "\n".join(str(path) for path in copilot_paths)
        statuses.extend([
            {
                "agent": "GitHub Copilot VS Code",
                "status": "missing",
                "details": "No VS Code settings.json file was found.",
                "path": searched,
            },
            {
                "agent": "GitHub Copilot CLI",
                "status": "missing",
                "details": "No VS Code settings.json file was found for the CLI env block.",
                "path": searched,
            },
        ])
    else:
        copilot_ready: Path | None = None
        copilot_issues: list[str] = []
        copilot_unreadable: list[str] = []
        copilot_cli_ready: Path | None = None
        copilot_cli_issues: list[str] = []
        copilot_cli_unreadable: list[str] = []
        for settings_path in existing_copilot_paths:
            try:
                settings = _json_loads(settings_path.read_text())
            except Exception as exc:
                copilot_unreadable.append(f"{settings_path.name}: {exc}")
                copilot_cli_unreadable.append(f"{settings_path.name}: {exc}")
                continue
            issues = _missing_desired_keys(settings, copilot_desired)
            if not issues and copilot_ready is None:
                copilot_ready = settings_path
            elif issues:
                copilot_issues.append(f"{settings_path.name}: {', '.join(issues)}")

            env_issues = _missing_desired_keys(settings.get("env"), copilot_cli_desired)
            if not env_issues and copilot_cli_ready is None:
                copilot_cli_ready = settings_path
            elif env_issues:
                copilot_cli_issues.append(f"{settings_path.name}: {', '.join(env_issues)}")

        statuses.append({
            "agent": "GitHub Copilot VS Code",
            "status": (
                "ready" if copilot_ready else "unreadable" if copilot_unreadable and not copilot_issues else "incomplete"
            ),
            "details": (
                "Native OTLP HTTP export is configured and content capture stays disabled."
                if copilot_ready
                else "Failed to read settings: " + "; ".join(copilot_unreadable)
                if copilot_unreadable and not copilot_issues
                else "Missing or incorrect settings: " + "; ".join(copilot_issues)
            ),
            "path": str(copilot_ready) if copilot_ready else "\n".join(str(path) for path in existing_copilot_paths),
        })
        statuses.append({
            "agent": "GitHub Copilot CLI",
            "status": (
                "ready"
                if copilot_cli_ready
                else "unreadable"
                if copilot_cli_unreadable and not copilot_cli_issues
                else "incomplete"
            ),
            "details": (
                "CLI env vars point the built-in exporter at the local OTLP HTTP gateway."
                if copilot_cli_ready
                else "Failed to read settings: " + "; ".join(copilot_cli_unreadable)
                if copilot_cli_unreadable and not copilot_cli_issues
                else "Missing or incorrect env keys: " + "; ".join(copilot_cli_issues)
            ),
            "path": (
                str(copilot_cli_ready)
                if copilot_cli_ready
                else "\n".join(str(path) for path in existing_copilot_paths)
            ),
        })

    gemini_path = Path.home() / ".gemini" / "settings.json"
    gemini_desired = _gemini_native_otel_settings(hook_config)
    if not gemini_path.exists():
        statuses.append({
            "agent": "Gemini CLI",
            "status": "missing",
            "details": "No settings.json found yet; reflect can only print env guidance until Gemini creates one.",
            "path": str(gemini_path),
        })
    else:
        try:
            gemini_settings = _json_loads(gemini_path.read_text())
        except Exception as exc:
            statuses.append({
                "agent": "Gemini CLI",
                "status": "unreadable",
                "details": f"Failed to read settings.json: {exc}",
                "path": str(gemini_path),
            })
        else:
            issues = _missing_desired_keys(gemini_settings.get("telemetry"), gemini_desired)
            statuses.append({
                "agent": "Gemini CLI",
                "status": "ready" if not issues else "incomplete",
                "details": (
                    "Local collector mode is enabled and prompt logging stays disabled."
                    if not issues
                    else "Missing or incorrect telemetry keys: " + ", ".join(issues)
                ),
                "path": str(gemini_path),
            })

    codex_path = Path.home() / ".codex" / "config.toml"
    codex_desired = _codex_native_otel_settings(hook_config)
    if not codex_path.exists():
        statuses.append({
            "agent": "OpenAI Codex CLI",
            "status": "missing",
            "details": "No config.toml found yet.",
            "path": str(codex_path),
        })
    else:
        try:
            codex_settings = tomllib.loads(codex_path.read_text())
        except Exception as exc:
            statuses.append({
                "agent": "OpenAI Codex CLI",
                "status": "unreadable",
                "details": f"Failed to read config.toml: {exc}",
                "path": str(codex_path),
            })
        else:
            otel = codex_settings.get("otel")
            issues = _missing_desired_keys(otel, codex_desired)
            statuses.append({
                "agent": "OpenAI Codex CLI",
                "status": "ready" if not issues else "incomplete",
                "details": (
                    "Explicit trace/log OTLP exporters are configured and prompt logging stays disabled."
                    if not issues
                    else "Missing or incorrect [otel] keys: " + ", ".join(issues)
                ),
                "path": str(codex_path),
            })

    return statuses


def _snapshot_detected_agent_configs(console, agents: list[dict]) -> None:
    for agent in agents:
        snapshots = []
        for source in _agent_config_candidates(agent):
            try:
                snapshots.append(_copy_config_snapshot(agent["name"], source))
            except Exception as exc:
                console.print(f"  [red]\u2717[/] Failed to snapshot {agent['name']} config {source}: {exc}")
        for snapshot in snapshots:
            console.print(f"  [green]\u2713[/] Saved {agent['name']} config snapshot \u2192 {snapshot}")


def _configure_claude_native_otel(console, hook_config: dict[str, str]) -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    desired_env = _claude_native_otel_env(hook_config)

    if settings_path.exists():
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Claude Code settings {settings_path}: {exc}")
            return
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    env = settings.get("env")
    changed = not isinstance(env, dict)
    if not isinstance(env, dict):
        env = {}
        settings["env"] = env
    for key, value in desired_env.items():
        if env.get(key) != value:
            env[key] = value
            changed = True

    if changed:
        settings_path.write_text(_json_stdlib.dumps(settings, indent=2) + "\n")
        console.print(f"  [green]\u2713[/] Enabled native Claude Code OTel in {settings_path}")
    else:
        console.print(f"  [green]\u2713[/] Native Claude Code OTel already enabled in {settings_path}")


def _configure_copilot_native_otel(console, hook_config: dict[str, str]) -> None:
    desired = _copilot_native_otel_settings(hook_config)

    searched_paths = _agent_config_paths({"name": "GitHub Copilot"})
    updated_any = False
    for settings_path in [path for path in searched_paths if path.exists()]:
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Copilot settings {settings_path}: {exc}")
            continue

        changed = False
        for key, value in desired.items():
            if settings.get(key) != value:
                settings[key] = value
                changed = True

        if changed:
            settings_path.write_text(_json_stdlib.dumps(settings, indent=2) + "\n")
            console.print(f"  [green]\u2713[/] Enabled native Copilot OTel in {settings_path}")
            updated_any = True
        else:
            console.print(f"  [green]\u2713[/] Native Copilot OTel already enabled in {settings_path}")
            updated_any = True

    if not updated_any:
        console.print("  [yellow]\u2022[/] Skipped native Copilot OTel: no VS Code settings.json file was found.")
        for path in searched_paths:
            console.print(f"    [dim]- {path}[/]")


def _configure_gemini_native_otel(console, hook_config: dict[str, str]) -> None:
    settings_path = Path.home() / ".gemini" / "settings.json"
    if not settings_path.exists():
        console.print("  [dim]\u2022[/] No Gemini CLI settings file detected; kept env guidance only.")
        return

    try:
        settings = _json_loads(settings_path.read_text())
    except Exception as exc:
        console.print(f"  [red]\u2717[/] Failed to read Gemini CLI settings {settings_path}: {exc}")
        return

    telemetry = settings.get("telemetry")
    changed = not isinstance(telemetry, dict)
    if not isinstance(telemetry, dict):
        telemetry = {}
        settings["telemetry"] = telemetry
    desired = _gemini_native_otel_settings(hook_config)

    for key, value in desired.items():
        if telemetry.get(key) != value:
            telemetry[key] = value
            changed = True

    if "outfile" in telemetry:
        telemetry.pop("outfile", None)
        changed = True

    if changed:
        settings_path.write_text(_json_stdlib.dumps(settings, indent=2) + "\n")
        console.print(f"  [green]\u2713[/] Enabled native Gemini telemetry in {settings_path}")
    else:
        console.print(f"  [green]\u2713[/] Native Gemini telemetry already enabled in {settings_path}")


def _configure_copilot_cli_native_otel(console, hook_config: dict[str, str]) -> None:
    """Set Copilot CLI OTel env vars in VS Code settings.json env block."""
    desired_env = _copilot_cli_native_otel_env(hook_config)

    searched_paths = _agent_config_paths({"name": "GitHub Copilot"})
    updated_any = False
    for settings_path in [path for path in searched_paths if path.exists()]:
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Copilot settings {settings_path}: {exc}")
            continue

        env = settings.get("env")
        changed = not isinstance(env, dict)
        if not isinstance(env, dict):
            env = {}
            settings["env"] = env
        for key, value in desired_env.items():
            if env.get(key) != value:
                env[key] = value
                changed = True

        if changed:
            settings_path.write_text(_json_stdlib.dumps(settings, indent=2) + "\n")
            console.print(f"  [green]\u2713[/] Enabled Copilot CLI OTel env vars in {settings_path}")
        else:
            console.print(f"  [green]\u2713[/] Copilot CLI OTel env vars already set in {settings_path}")
        updated_any = True

    if not updated_any:
        console.print("  [yellow]\u2022[/] Skipped Copilot CLI OTel env vars: no VS Code settings.json file was found.")
        for path in searched_paths:
            console.print(f"    [dim]- {path}[/]")


def _configure_codex_native_otel(console, hook_config: dict[str, str]) -> None:
    """Write [otel] section to ~/.codex/config.toml (interactive mode only)."""
    config_path = Path.home() / ".codex" / "config.toml"
    desired_otel = _codex_native_otel_settings(hook_config)

    if config_path.exists():
        try:
            existing = tomllib.loads(config_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Codex config {config_path}: {exc}")
            return
        existing_otel = existing.get("otel")
        if _codex_native_otel_matches_desired(existing_otel, desired_otel):
            console.print(f"  [green]\u2713[/] Native Codex OTel already enabled in {config_path}")
            return
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    # Append or replace [otel] section — read original text to preserve other sections
    original = config_path.read_text() if config_path.exists() else ""
    otel_block = _render_codex_native_otel_block(hook_config)
    updated = _upsert_toml_section(original, "otel", otel_block)

    config_path.write_text(updated)
    console.print(f"  [green]\u2713[/] Enabled native Codex OTel in {config_path}")




@main.command()
def setup() -> None:
    """Install opentelemetry-hooks, configure local data export, and suggest agent enablement."""
    from rich.console import Console
    console = Console(force_terminal=True)

    console.print("\n[bold cyan]reflect setup[/]\n")
    console.print("[dim]Prepare local telemetry capture, wire supported agents, and leave clear next steps.[/]")

    console.print("\n[bold]Step 1: Prepare reflect home[/]")
    for subdir in ("state", "state/local_spans", "state/sessions", "reports", "agents"):
        (REFLECT_HOME / subdir).mkdir(parents=True, exist_ok=True)
    console.print(f"  [green]\u2713[/] Created [bold]{REFLECT_HOME}[/]")

    detected_agents = [agent for agent in _detect_agents() if agent["detected"]]
    if detected_agents:
        console.print("\n[bold]Step 2: Snapshot detected agent configs[/]")
        _snapshot_detected_agent_configs(console, detected_agents)

    console.print("\n[bold]Step 3: Install or verify opentelemetry-hooks[/]")
    otel_hook = shutil.which("otel-hook")
    if otel_hook:
        console.print(f"  [green]\u2713[/] opentelemetry-hooks already installed ({otel_hook})")
    else:
        console.print("  [yellow]\u2022[/] Installing opentelemetry-hooks via pipx...")
        try:
            subprocess.check_call(
                ["pipx", "install", _HOOK_PACKAGE_SPEC],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            otel_hook = shutil.which("otel-hook")
            console.print(f"  [green]\u2713[/] Installed opentelemetry-hooks ({otel_hook})")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            console.print(f"  [red]\u2717[/] Failed to install opentelemetry-hooks: {exc}")
            console.print(f"    Install manually: [bold]pipx install {_HOOK_PACKAGE_SPEC}[/]")

    console.print("\n[bold]Step 4: Configure local telemetry export[/]")
    config_path = HOOK_HOME / "otel_config.json"
    if config_path.exists():
        backup = _copy_config_snapshot("opentelemetry-hooks", config_path)
        console.print(f"  [green]\u2713[/] Saved hook config snapshot \u2192 {backup}")
        config = _json_loads(config_path.read_text())
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        example_path = HOOK_HOME / "otel_config.example.json"
        config = _json_loads(example_path.read_text()) if example_path.exists() else {}

    config["IDE_OTEL_LOCAL_SPANS"] = "true"
    config.setdefault("IDE_OTEL_ENABLE_LOGS", "true")
    config.setdefault("IDE_OTEL_BATCH_ON_STOP", "true")
    config.setdefault("OTEL_SERVICE_NAME", "ide-agent")
    config.setdefault("IDE_OTEL_APP_NAME", "ide-agent")
    config.setdefault("IDE_OTEL_SUBSYSTEM_NAME", "ide-hooks")
    config.setdefault(_HOOK_CFG_ENDPOINT_KEY, _HOOK_CFG_ENDPOINT_DEFAULT)
    config.setdefault(_HOOK_CFG_PROTOCOL_KEY, _HOOK_CFG_PROTOCOL_DEFAULT)
    config_path.write_text(_json_stdlib.dumps(config, indent=2) + "\n")
    console.print(f"  [green]\u2713[/] Hook config updated ({config_path})")

    # Symlink the hook's local_spans dir into ~/.reflect/state/ if different
    hook_spans_dir = HOOK_HOME / ".state" / "local_spans"
    reflect_spans_dir = REFLECT_HOME / "state" / "local_spans"
    if hook_spans_dir.resolve() != reflect_spans_dir.resolve():
        # Remove the empty dir we created and symlink to the hook's dir
        if reflect_spans_dir.is_dir() and not reflect_spans_dir.is_symlink() and not any(reflect_spans_dir.iterdir()):
            reflect_spans_dir.rmdir()
        if not reflect_spans_dir.exists():
            hook_spans_dir.mkdir(parents=True, exist_ok=True)
            reflect_spans_dir.symlink_to(hook_spans_dir)
            console.print(f"  [green]\u2713[/] Linked local_spans → {hook_spans_dir}")

    # Symlink sessions too
    hook_sessions_dir = HOOK_HOME / ".state" / "sessions"
    reflect_sessions_dir = REFLECT_HOME / "state" / "sessions"
    if hook_sessions_dir.resolve() != reflect_sessions_dir.resolve():
        if reflect_sessions_dir.is_dir() and not reflect_sessions_dir.is_symlink() and not any(reflect_sessions_dir.iterdir()):
            reflect_sessions_dir.rmdir()
        if not reflect_sessions_dir.exists():
            hook_sessions_dir.mkdir(parents=True, exist_ok=True)
            reflect_sessions_dir.symlink_to(hook_sessions_dir)
            console.print(f"  [green]\u2713[/] Linked sessions → {hook_sessions_dir}")

    # Keep the canonical OTLP traces cache aligned with the workspace file when present.
    ws_traces_otlp = Path.cwd() / "reflect" / "state" / "otlp" / "otel-traces.json"
    ws_traces_root = Path.cwd() / "reflect" / "state" / "otel-traces.json"
    ws_traces = ws_traces_otlp if ws_traces_otlp.exists() else ws_traces_root

    home_traces = _canonical_otlp_traces_path()
    home_traces.parent.mkdir(parents=True, exist_ok=True)
    if ws_traces.exists() and not home_traces.exists():
        home_traces.symlink_to(ws_traces)
        console.print(f"  [green]\u2713[/] Linked workspace traces \u2192 {ws_traces}")

    ws_logs_otlp = Path.cwd() / "reflect" / "state" / "otlp" / "otel-logs.json"
    ws_logs_root = Path.cwd() / "reflect" / "state" / "otel-logs.json"
    ws_logs = ws_logs_otlp if ws_logs_otlp.exists() else ws_logs_root

    home_logs = REFLECT_HOME / "state" / "otel-logs.json"
    if ws_logs.exists() and not home_logs.exists():
        home_logs.symlink_to(ws_logs)
        console.print(f"  [green]\u2713[/] Linked workspace logs \u2192 {ws_logs}")

    # 5. Delegate hook-based agent wiring to opentelemetry-hooks
    console.print("\n[bold]Step 5: Wire hook-based agents via opentelemetry-hooks[/]")
    if otel_hook:
        try:
            subprocess.check_call([otel_hook, "setup", "--global"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            console.print("  [green]\u2713[/] opentelemetry-hooks setup complete")
        except subprocess.CalledProcessError as exc:
            console.print(f"  [red]\u2717[/] opentelemetry-hooks setup failed (exit {exc.returncode})")
            console.print("    Run manually: [bold]otel-hook setup[/]")
    else:
        console.print("  [yellow]\u2022[/] otel-hook not found; skipping hook-based agent wiring")
        console.print(f"    Install first: [bold]pipx install {_HOOK_PACKAGE_SPEC}[/]")

    # 6. Configure native OTel for all agents that have built-in OTLP export.
    # otel-hook setup (step 5) handles hook-based agents; this step handles native OTel.
    console.print("\n[bold]Step 6: Enable native OTel (Claude Code, Copilot, Gemini, Codex)[/]")
    _configure_claude_native_otel(console, config)
    _configure_copilot_native_otel(console, config)
    _configure_copilot_cli_native_otel(console, config)
    _configure_gemini_native_otel(console, config)
    _configure_codex_native_otel(console, config)

    # 6b. Start local OTLP gateway (receives native OTel from agents)
    console.print("\n[bold]Step 6b: Start local OTLP gateway[/]")
    from reflect.gateway import _is_running as _gateway_is_running
    from reflect.gateway import daemon_start as _gateway_daemon_start

    if _gateway_is_running():
        console.print("  [green]\u2713[/] Gateway already running")
    else:
        try:
            pid = _gateway_daemon_start()
            console.print(f"  [green]\u2713[/] Gateway started (PID {pid}) — gRPC :4317 | HTTP :4318")
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to start gateway: {exc}")
            console.print("    Start manually: [bold]reflect gateway start[/]")

    planned_agents = [agent for agent in detected_agents if agent.get("support_status") != "Implemented"]
    if planned_agents:
        console.print("\n[bold yellow]Telemetry gaps still not implemented[/]")
        for agent in planned_agents:
            console.print(
                f"  [yellow]\u2022[/] {agent['name']}: {agent['telemetry_path']}. "
                "reflect setup will not start collecting telemetry for this agent yet."
            )

    # 7. Distribute Skills
    console.print("\n[bold]Step 7: Distribute AI Agent Skills[/]")
    _distribute_skills(console)

    # 8. Summary
    console.print("\n[bold]Step 8: Next steps[/]")
    console.print(f"[bold green]Done![/] Data will be written to [bold]{REFLECT_HOME}/state/[/]")
    console.print("\nRun [bold]reflect doctor[/] to confirm capture health, then run [bold]reflect[/] to view your dashboard.")
    console.print()


@main.command()
def doctor() -> None:
    """Inspect local reflect, hook, and agent state and suggest the next telemetry step."""
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console(force_terminal=True)
    otel_hook = shutil.which("otel-hook")
    hook_config = HOOK_HOME / "otel_config.json"
    spans_dir = _default_spans_dir()
    sessions_dir = _default_sessions_dir()
    otlp_traces = _default_otlp_traces()
    otlp_logs = _infer_default_otlp_logs()
    agents = _detect_agents()
    detected_agents = [agent for agent in agents if agent["detected"]]
    span_files = _count_glob(spans_dir, "*.jsonl")
    session_files = _count_glob(sessions_dir, "*.json")
    update_advisor = _collect_update_advisor(allow_remote=True)
    hook_runtime_config: dict[str, str] = {}
    if hook_config.exists():
        try:
            loaded_hook_config = _json_loads(hook_config.read_text())
        except Exception:
            loaded_hook_config = {}
        if isinstance(loaded_hook_config, dict):
            hook_runtime_config = loaded_hook_config

    def _status_markup(ok: bool, present: str = "present", missing: str = "missing") -> str:
        return f"[green]{present}[/]" if ok else f"[red]{missing}[/]"

    def _native_status_markup(status: str) -> str:
        if status == "ready":
            return "[green]ready[/]"
        if status == "incomplete":
            return "[yellow]incomplete[/]"
        return "[red]missing[/]" if status == "missing" else "[red]unreadable[/]"

    console.print()
    console.print(
        Panel.fit(
            "[bold cyan]reflect doctor[/]\n[dim]Inspect local capture health, telemetry files, and supported agent homes.[/]",
            border_style="cyan",
        )
    )

    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column()
    summary.add_row("reflect home", _status_markup(REFLECT_HOME.exists()))
    summary.add_row("otel-hook", _status_markup(bool(otel_hook), present="installed", missing="missing"))
    summary.add_row("hook config", _status_markup(hook_config.exists()))
    summary.add_row("detected agents", f"[bold]{len(detected_agents)}[/] / {len(agents)}")
    summary.add_row("local spans", f"[bold]{span_files}[/] file(s)")
    summary.add_row("local sessions", f"[bold]{session_files}[/] file(s)")
    from reflect.gateway import _is_running as _gw_running
    gw_pid = _gw_running()
    summary.add_row(
        "otlp gateway",
        _status_markup(gw_pid is not None, present=f"running (PID {gw_pid})", missing="stopped"),
    )
    console.print(Panel(summary, title="Overview", border_style="blue"))
    _render_update_advisor_panel(console, update_advisor)

    exports = Table(box=box.SIMPLE_HEAVY, expand=True)
    exports.add_column("Signal", style="bold cyan")
    exports.add_column("Status", no_wrap=True)
    exports.add_column("Details")
    exports.add_column("Path", overflow="fold")
    exports.add_row(
        "OTLP traces",
        _status_markup(bool(otlp_traces and otlp_traces.exists()), present="ready"),
        _summarize_file(otlp_traces),
        str(otlp_traces or _canonical_otlp_traces_path()),
    )
    if otlp_logs and otlp_logs.exists():
        exports.add_row(
            "OTLP logs",
            _status_markup(True, present="ready"),
            _summarize_file(otlp_logs),
            str(otlp_logs),
        )
    else:
        if otel_hook:
            exports.add_row(
                "OTLP logs",
                "[yellow]waiting[/]",
                "otel-hook log export is enabled (IDE_OTEL_ENABLE_LOGS); no log file written yet",
                str(REFLECT_HOME / "state" / "otel-logs.json"),
            )
        else:
            exports.add_row(
                "OTLP logs",
                "[red]missing[/]",
                "Install otel-hook to enable log capture (IDE_OTEL_ENABLE_LOGS)",
                str(REFLECT_HOME / "state" / "otel-logs.json"),
            )
    exports.add_row(
        "Hook spans",
        _status_markup(span_files > 0, present="capturing"),
        f"{span_files} jsonl file(s)",
        str(spans_dir),
    )
    exports.add_row(
        "Hook sessions",
        _status_markup(session_files > 0, present="capturing"),
        f"{session_files} json file(s)",
        str(sessions_dir),
    )
    console.print(Panel(exports, title="Telemetry files", border_style="magenta"))

    native_otel = Table(box=box.SIMPLE_HEAVY, expand=True)
    native_otel.add_column("Agent", style="bold cyan")
    native_otel.add_column("Status", no_wrap=True)
    native_otel.add_column("Details")
    native_otel.add_column("Path", overflow="fold")
    for status in _collect_native_otel_statuses(hook_runtime_config):
        native_otel.add_row(
            status["agent"],
            _native_status_markup(status["status"]),
            status["details"],
            status["path"],
        )
    console.print(Panel(native_otel, title="Native agent telemetry", border_style="cyan"))

    if detected_agents:
        detected_table = Table(box=box.SIMPLE_HEAVY, expand=True)
        detected_table.add_column("Agent", style="bold")
        detected_table.add_column("Entries", justify="right", no_wrap=True)
        detected_table.add_column("Skills", justify="right", no_wrap=True)
        detected_table.add_column("Path", overflow="fold")
        detected_table.add_column("Recommended next step", overflow="fold")
        for agent in detected_agents:
            skills_count = 0
            global_skills_path = Path(agent["global_path"]).expanduser()
            try:
                if global_skills_path.is_dir():
                    skills_count = sum(1 for p in global_skills_path.iterdir() if p.is_dir())
            except OSError:
                pass
            detected_table.add_row(
                agent["name"],
                str(agent["entries"]),
                str(skills_count) if skills_count else "—",
                f"{agent['path']} [dim]({agent['env']})[/]",
                agent["recommendation"],
            )
        console.print(Panel(detected_table, title="Detected agent homes", border_style="green"))
    else:
        console.print(
            Panel(
                "[dim]No supported agent homes were detected yet.[/]\nRun [cyan]reflect setup[/] first, or point agent env overrides at existing homes.",
                title="Detected agent homes",
                border_style="green",
            )
        )

    integrations = Table(box=box.SIMPLE, expand=True, show_header=True)
    integrations.add_column("Integration", style="bold cyan")
    integrations.add_column("Env override", style="dim", no_wrap=True)
    integrations.add_column("Status", no_wrap=True)
    integrations.add_column("Telemetry path")
    integrations.add_column("Confidence", no_wrap=True)
    for agent in agents:
        integrations.add_row(
            agent["name"],
            agent["env"],
            agent["support_status"],
            agent["telemetry_path"],
            agent["confidence"],
        )
    console.print(Panel(integrations, title="Support matrix", border_style="green"))

    if otlp_traces and otlp_traces.exists():
        action_line = f"[bold]Try now:[/] [cyan]reflect --otlp-traces {otlp_traces}[/]"
    else:
        action_line = "[bold]Next:[/] run [cyan]reflect setup[/] or enable native telemetry on a supported agent."

    next_steps_lines = [
        "- [bold]Use native telemetry first[/] where the agent supports it well.",
        "- Use [bold]hooks[/] when you need control, auditability, or process-boundary coverage.",
        "- Use [bold]session/log adapters[/] first for weaker or desktop-only integrations.",
    ]
    if any(agent["name"] == "Cursor" for agent in detected_agents):
        next_steps_lines.append(
            "- [bold]Cursor desktop[/] adapters explain flow and tools, but exact per-session tokens may still require provider-side usage context."
        )
    next_steps_lines.extend(["", action_line])

    console.print(
        Panel(
            "\n".join(next_steps_lines),
            title="Suggested next steps",
            border_style="yellow",
        )
    )
    console.print()


@main.command()
@click.option(
    "--apply",
    is_flag=True,
    help="Attempt a package upgrade via pipx when a newer reflect release is available.",
)
def update(apply: bool) -> None:
    """Check reflect release drift and show concrete repair steps."""
    from rich.console import Console

    console = Console(force_terminal=True)
    advisor = _collect_update_advisor(allow_remote=True)

    console.print()
    console.print("[bold cyan]reflect update[/]\n")
    _render_update_advisor_panel(console, advisor)

    release = advisor["release"]
    if apply:
        if release["update_available"]:
            pipx = shutil.which("pipx")
            if not pipx:
                console.print("[red]pipx is not installed or not on PATH.[/]")
                console.print("Install pipx, then run [bold]pipx upgrade o11y-reflect[/].")
                raise SystemExit(1)
            try:
                subprocess.check_call([pipx, "upgrade", "o11y-reflect"])
                console.print("[green]Package upgrade finished.[/] Re-run [bold]reflect doctor[/] to refresh the cached status.")
            except subprocess.CalledProcessError as exc:
                console.print(f"[red]pipx upgrade failed:[/] {exc}")
                raise SystemExit(exc.returncode or 1) from exc
        else:
            console.print("[green]No newer package release is available right now.[/]")

        if advisor["local_issues"]:
            console.print("Local drift remains. Run [bold]reflect setup[/] from the workspace root to refresh hooks and skill copies.")
    else:
        console.print("Use [bold]reflect update --apply[/] to upgrade the package when a newer release is available.")
        if advisor["local_issues"]:
            console.print("For local hook or skill drift, run [bold]reflect setup[/] from the workspace root.")
    console.print()


@main.group(invoke_without_command=True)
@click.option("--grpc-port", type=int, default=4317, help="gRPC listen port (default 4317).")
@click.option("--http-port", type=int, default=4318, help="HTTP listen port (default 4318).")
@click.option("--foreground", is_flag=True, help="Run the gateway in the foreground (blocking).")
@click.pass_context
def gateway(ctx, grpc_port: int, http_port: int, foreground: bool) -> None:
    """Local OTLP gateway — receive traces and logs from agents, write to local files."""
    ctx.ensure_object(dict)
    ctx.obj["grpc_port"] = grpc_port
    ctx.obj["http_port"] = http_port
    if ctx.invoked_subcommand is not None:
        return
    if foreground:
        from reflect.gateway import start_gateway

        start_gateway(grpc_port=grpc_port, http_port=http_port)
    else:
        # Default (no subcommand, no --foreground): start as daemon
        ctx.invoke(gateway_start)


@gateway.command("start")
@click.pass_context
def gateway_start(ctx) -> None:
    """Start the gateway as a background daemon."""
    from rich.console import Console

    from reflect.gateway import daemon_start

    console = Console(force_terminal=True)
    grpc_port = ctx.obj["grpc_port"]
    http_port = ctx.obj["http_port"]
    try:
        pid = daemon_start(grpc_port=grpc_port, http_port=http_port)
    except RuntimeError as exc:
        console.print(f"[yellow]{exc}[/]")
        return
    console.print(
        f"[green]\u2713[/] Gateway started (PID {pid})"
        f"  gRPC :{grpc_port}  HTTP :{http_port}"
    )


@gateway.command("stop")
def gateway_stop() -> None:
    """Stop the background gateway daemon."""
    from rich.console import Console

    from reflect.gateway import daemon_stop

    console = Console(force_terminal=True)
    if daemon_stop():
        console.print("[green]\u2713[/] Gateway stopped")
    else:
        console.print("[yellow]Gateway is not running[/]")


@gateway.command("status")
def gateway_status() -> None:
    """Show gateway daemon status."""
    from rich.console import Console

    from reflect.gateway import daemon_status

    console = Console(force_terminal=True)
    status = daemon_status()
    if status["running"]:
        console.print(f"[green]running[/] (PID {status['pid']})")
    else:
        console.print("[red]stopped[/]")
    console.print(f"  traces: {status['traces_path']} ({_summarize_file(Path(status['traces_path']))})")
    console.print(f"  logs:   {status['logs_path']} ({_summarize_file(Path(status['logs_path']))})")
    console.print(f"  log:    {status['log_file']}")


if __name__ == "__main__":
    main()
