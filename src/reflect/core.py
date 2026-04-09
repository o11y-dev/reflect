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

    # Open the hosted dashboard view with encoded data
    python3 src/reflect/core.py \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json --publish

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
]


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
    except Exception:
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
    "--publish",
    is_flag=True,
    help="Open the dashboard in a browser. Starts a local server and loads the data via ?report=... (no URL encoding).",
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
    publish: bool,
    dashboard_artifact: Path | None,
    demo: bool,
    time_range: str,
) -> None:
    """AI usage telemetry report — analyze OpenTelemetry span data from your AI sessions."""
    if ctx.invoked_subcommand is not None:
        return

    # --demo: use bundled sample data and force --all time range
    if demo:
        _demo_traces = Path(__file__).parent / "data" / "demo-traces.json"
        if not _demo_traces.exists():
            # Fallback to repo-level state/ for development installs
            _demo_traces = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
        if not _demo_traces.exists():
            click.echo("Demo data not found. Re-install the package or run from the repo root.", err=True)
            raise SystemExit(1)
        otlp_traces = _demo_traces
        sessions_dir = sessions_dir or Path(os.devnull)
        spans_dir = spans_dir or Path(os.devnull)
        time_range = "all"

    # Compute time cutoff
    since: datetime | None = None
    if time_range != "all":
        from datetime import timedelta
        now = datetime.now(tz=UTC)
        deltas = {"day": timedelta(days=1), "week": timedelta(days=7), "month": timedelta(days=30)}
        since = now - deltas[time_range]

    # Resolve defaults
    if sessions_dir is None:
        sessions_dir = _default_sessions_dir()
    if spans_dir is None:
        spans_dir = _default_spans_dir()
    if otlp_traces is None:
        otlp_traces = _default_otlp_traces()
    if output is None:
        out_dir = REFLECT_HOME / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / f"ai-usage-telemetry-report-{datetime.now().strftime('%Y-%m-%d')}.md"

    update_notice = _build_startup_update_notice()
    if update_notice:
        click.echo(f"reflect notice: {update_notice}")

    stats = analyze_telemetry(sessions_dir, spans_dir, otlp_traces, since=since)
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

        publish_artifact = dashboard_artifact
        publish_url: str | None = None
        if publish:
            if publish_artifact is None:
                publish_artifact = _default_publish_artifact_path()
                publish_artifact.parent.mkdir(parents=True, exist_ok=True)
                _write_dashboard_artifact(stats, publish_artifact)
            publish_url = _publish_url_for_artifact(publish_artifact)
            if publish_url is None:
                publish_url = "http://127.0.0.1:8765/?report=api/data"

        print(f"Report saved to:   {output}")
        if publish_artifact is not None:
            print(f"Dashboard JSON:    {publish_artifact}")
        if publish_url is not None:
            print(f"Dashboard URL:     {publish_url}")
        print(f"Analyzed events:   {stats.total_events:,}")
        print(f"Sessions:          {len(stats.sessions_seen)} unique")
        print(f"Active days:       {stats.days_active}")
        print(f"Top model:         {stats.models_by_count.most_common(1)[0][0] if stats.models_by_count else 'N/A'}")
        print(f"Tool-to-prompt:    {_safe_ratio(stats.events_by_type.get('PreToolUse', 0), stats.events_by_type.get('UserPromptSubmit', 0)):.1f}:1")
        if publish_url is not None:
            import webbrowser
            webbrowser.open(publish_url)
    if publish and terminal:
        # Starts FastAPI server — blocks until Ctrl-C
        _start_publish_server(stats)


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
    """Distribute reflect and opentelemetry-skill to detected agents."""
    # reflect skill is bundled with the package
    bundled_skills_dir = Path(__file__).parent / "data" / "skills"

    available_skills: dict[str, Path] = {}

    reflect_skill = bundled_skills_dir / "reflect"
    if (reflect_skill / "SKILL.md").exists():
        available_skills["reflect"] = reflect_skill

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


def _agent_config_candidates(agent: dict) -> list[Path]:
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
    return [path for path in candidates if path.exists()]


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
    endpoint = hook_config.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    protocol = hook_config.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")

    desired_env = {
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
    }

    if settings_path.exists():
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Claude Code settings {settings_path}: {exc}")
            return
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    env = settings.setdefault("env", {})
    changed = False
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
    endpoint = hook_config.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    copilot_endpoint = endpoint.replace(":4317", ":4318") if endpoint.endswith(":4317") else endpoint
    desired = {
        "github.copilot.chat.otel.enabled": True,
        "github.copilot.chat.otel.otlpEndpoint": copilot_endpoint,
        "github.copilot.chat.otel.exporterType": "otlp-http",
        "github.copilot.chat.otel.captureContent": False,
    }

    updated_any = False
    for settings_path in _agent_config_candidates({"name": "GitHub Copilot"}):
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
        console.print("  [dim]\u2022[/] No VS Code Copilot settings files detected; kept env guidance only.")


def _configure_gemini_native_otel(console, hook_config: dict[str, str]) -> None:
    settings_path = Path.home() / ".gemini" / "settings.json"
    if not settings_path.exists():
        console.print("  [dim]\u2022[/] No Gemini CLI settings file detected; kept env guidance only.")
        return

    endpoint = hook_config.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    protocol = hook_config.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc")
    gemini_protocol = "http" if protocol.startswith("http") else "grpc"

    try:
        settings = _json_loads(settings_path.read_text())
    except Exception as exc:
        console.print(f"  [red]\u2717[/] Failed to read Gemini CLI settings {settings_path}: {exc}")
        return

    telemetry = settings.setdefault("telemetry", {})
    desired = {
        "enabled": True,
        "target": "local",
        "useCollector": True,
        "otlpEndpoint": endpoint,
        "otlpProtocol": gemini_protocol,
        "logPrompts": False,
    }

    changed = False
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
    endpoint = hook_config.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    copilot_endpoint = endpoint.replace(":4317", ":4318") if endpoint.endswith(":4317") else endpoint

    desired_env = {
        "COPILOT_OTEL_ENABLED": "true",
        "COPILOT_OTEL_OTLP_ENDPOINT": copilot_endpoint,
    }

    updated_any = False
    for settings_path in _agent_config_candidates({"name": "GitHub Copilot"}):
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Copilot settings {settings_path}: {exc}")
            continue

        env = settings.setdefault("env", {})
        changed = False
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
        console.print("  [dim]\u2022[/] No VS Code Copilot settings files detected; skipping Copilot CLI env vars.")


def _configure_codex_native_otel(console, hook_config: dict[str, str]) -> None:
    """Write [otel] section to ~/.codex/config.toml (interactive mode only)."""
    config_path = Path.home() / ".codex" / "config.toml"
    endpoint = hook_config.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    if config_path.exists():
        try:
            existing = tomllib.loads(config_path.read_text())
        except Exception as exc:
            console.print(f"  [red]\u2717[/] Failed to read Codex config {config_path}: {exc}")
            return
        existing_otel = existing.get("otel", {})
        already_set = (
            existing_otel.get("log_user_prompt") is False
            and "otlp-grpc" in str(existing_otel.get("exporter", ""))
        )
        if already_set:
            console.print(f"  [green]\u2713[/] Native Codex OTel already enabled in {config_path}")
            return
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    # Append or replace [otel] section — read original text to preserve other sections
    original = config_path.read_text() if config_path.exists() else ""
    otel_block = (
        "\n[otel]\n"
        f'exporter = {{otlp-grpc = {{endpoint = "{endpoint}"}}}}\n'
        "log_user_prompt = false\n"
    )

    if re.search(r"^\[otel\]", original, re.MULTILINE):
        # Replace existing [otel] section (up to next section or end of file)
        updated = re.sub(
            r"\[otel\].*?(?=\n\[|\Z)", otel_block.strip() + "\n", original,
            flags=re.DOTALL,
        )
    else:
        updated = original.rstrip("\n") + otel_block

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
                ["pipx", "install", "opentelemetry-hooks"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            otel_hook = shutil.which("otel-hook")
            console.print(f"  [green]\u2713[/] Installed opentelemetry-hooks ({otel_hook})")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            console.print(f"  [red]\u2717[/] Failed to install opentelemetry-hooks: {exc}")
            console.print("    Install manually: [bold]pipx install opentelemetry-hooks[/]")

    console.print("\n[bold]Step 4: Configure local telemetry export[/]")
    config_path = HOOK_HOME / "otel_config.json"
    if config_path.exists():
        backup = _copy_config_snapshot("opentelemetry-hooks", config_path)
        console.print(f"  [green]\u2713[/] Saved hook config snapshot \u2192 {backup}")
        config = _json_loads(config_path.read_text())
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = {}

    config["IDE_OTEL_LOCAL_SPANS"] = "true"
    config.setdefault("IDE_OTEL_BATCH_ON_STOP", "true")
    config.setdefault("OTEL_SERVICE_NAME", "ide-agent")
    config.setdefault("IDE_OTEL_APP_NAME", "ide-agent")
    config.setdefault("IDE_OTEL_SUBSYSTEM_NAME", "ide-hooks")
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
            subprocess.check_call([otel_hook, "setup"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            console.print("  [green]\u2713[/] opentelemetry-hooks setup complete")
        except subprocess.CalledProcessError as exc:
            console.print(f"  [red]\u2717[/] opentelemetry-hooks setup failed (exit {exc.returncode})")
            console.print("    Run manually: [bold]otel-hook setup[/]")
    else:
        console.print("  [yellow]\u2022[/] otel-hook not found; skipping hook-based agent wiring")
        console.print("    Install first: [bold]pipx install opentelemetry-hooks[/]")

    # 6. Configure native OTel for all agents that have built-in OTLP export.
    # otel-hook setup (step 5) handles hook-based agents; this step handles native OTel.
    console.print("\n[bold]Step 6: Enable native OTel (Claude Code, Copilot, Gemini, Codex)[/]")
    _configure_claude_native_otel(console, config)
    _configure_copilot_native_otel(console, config)
    _configure_copilot_cli_native_otel(console, config)
    _configure_gemini_native_otel(console, config)
    _configure_codex_native_otel(console, config)

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

    def _status_markup(ok: bool, present: str = "present", missing: str = "missing") -> str:
        return f"[green]{present}[/]" if ok else f"[red]{missing}[/]"

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
    exports.add_row(
        "OTLP logs",
        _status_markup(bool(otlp_logs and otlp_logs.exists()), present="ready"),
        _summarize_file(otlp_logs),
        str(otlp_logs or (REFLECT_HOME / "state" / "otel-logs.json")),
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

    if detected_agents:
        detected_table = Table(box=box.SIMPLE_HEAVY, expand=True)
        detected_table.add_column("Agent", style="bold")
        detected_table.add_column("Entries", justify="right", no_wrap=True)
        detected_table.add_column("Path", overflow="fold")
        detected_table.add_column("Recommended next step", overflow="fold")
        for agent in detected_agents:
            detected_table.add_row(
                agent["name"],
                str(agent["entries"]),
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
    integrations.add_column("Supported integrations", style="bold cyan")
    integrations.add_column("Env override", style="dim", no_wrap=True)
    integrations.add_column("Default strategy")
    for agent in agents:
        strategy = agent["recommendation"].split(";")[0].split(".")[0]
        integrations.add_row(agent["name"], agent["env"], strategy)
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


if __name__ == "__main__":
    main()
