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

    # Open the local browser report
    python3 src/reflect/core.py \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json

    # Legacy markdown report
    python3 src/reflect/core.py \\
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json --output reports/my-report.md

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
import sqlite3
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
from pathlib import Path
from typing import TYPE_CHECKING
from urllib import error as urllib_error
from urllib import request as urllib_request

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

if TYPE_CHECKING:
    from reflect.improvements.service import ImprovementService
    from reflect.memory import MemoryService
    from reflect.preparation import (
        PreparationProgressReporter,
        SnapshotPreparationResult,
        SnapshotReadinessProbe,
        SnapshotStatus,
    )

# ---------------------------------------------------------------------------
# Reflect home directory
# ---------------------------------------------------------------------------

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
HOOK_HOME = Path(os.environ.get("IDE_OTEL_HOOK_HOME",
                                 Path.home() / ".local" / "share" / "opentelemetry-hooks"))

from reflect.dashboard import _start_publish_server
from reflect.hook_runtime import HookMigrationError, HookPipxMigrator, HookRuntime
from reflect.instrumentation import (  # noqa: F401
    _HOOK_CFG_ENDPOINT_DEFAULT,
    _HOOK_CFG_ENDPOINT_KEY,
    _HOOK_CFG_PROTOCOL_DEFAULT,
    _HOOK_CFG_PROTOCOL_KEY,
    _HOOK_PACKAGE_SPEC,
    _agent_config_candidates,
    _agent_config_paths,
    _agent_slug,
    _claude_native_otel_env,
    _codex_native_otel_matches_desired,
    _codex_native_otel_settings,
    _collect_native_otel_statuses,
    _configure_claude_native_otel,
    _configure_codex_native_otel,
    _configure_copilot_cli_native_otel,
    _configure_copilot_native_otel,
    _configure_gemini_native_otel,
    _copilot_cli_native_otel_env,
    _copilot_native_otel_settings,
    _gemini_native_otel_settings,
    _hook_otlp_endpoint,
    _hook_otlp_protocol,
    _missing_desired_keys,
    _native_otel_target,
    _render_codex_native_otel_block,
    _render_native_otel_panel,
    _upsert_toml_section,
)
from reflect.instrumentation import (
    _run_setup as _instrumentation_run_setup,
)
from reflect.mcp_clients import get_mcp_client_capability
from reflect.models import TelemetryStats
from reflect.parsing import (
    _canonical_otlp_traces_path,
    _default_sessions_dir,
    _default_spans_dir,
    _discover_rich_session_files,
    _infer_otlp_logs_file,
    _native_session_path_matches_id,
)
from reflect.processing import analyze_telemetry
from reflect.report import render_report
from reflect.shell_completion import (
    SUPPORTED_SHELLS,
    ShellCompletionManager,
    complete_loop_id,
    complete_memory_candidate_id,
    complete_memory_id,
    complete_memory_provider,
    complete_memory_scope,
    complete_memory_source,
    complete_memory_type,
    complete_observation_id,
    complete_session_id,
    complete_skill_id,
    complete_workflow_id,
)
from reflect.skill_extraction import (
    _build_skill_evidence_bundle,
    _build_skill_evidence_bundle_from_sql,
    _build_skills_extraction_prompt_from_bundle,
    _load_extracted_skills,
)
from reflect.utils import _json_loads, logger

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
        "setup_aliases": ["claude"],
        "env": "CLAUDE_HOME",
        "default": lambda: Path.home() / ".claude",
        "path_kind": "home",
        "local_skill_path": ".claude/skills/",
        "hook_agent": "claude",
        "global_path": "~/.claude/skills/",
        "recommendation": "Run reflect setup to wire Claude hooks and enable native Claude telemetry.",
    },
    {
        "name": "Cursor",
        "setup_aliases": ["cursor-agent"],
        "env": "CURSOR_HOME",
        "default": lambda: Path.home() / ".cursor",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "hook_agent": "cursor",
        "global_path": "~/.cursor/skills/",
        "recommendation": "Use session/log adapters for desktop; hooks help for headless or CLI launches. Treat state.vscdb as auth/context, not a guaranteed per-session token ledger.",
    },
    {
        "name": "Gemini CLI",
        "setup_aliases": ["gemini"],
        "env": "GEMINI_HOME",
        "env_aliases": ["GEMINI_DIR"],
        "default": lambda: Path.home() / ".gemini",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "hook_agent": "gemini",
        "global_path": "~/.gemini/skills/",
        "recommendation": "Prefer native Gemini OTel; keep session/log adapters for troubleshooting.",
    },
    {
        "name": "GitHub Copilot",
        "setup_aliases": ["copilot"],
        "env": "COPILOT_HOME",
        "default": lambda: Path.home() / ".copilot",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "hook_agent": "copilot",
        "global_path": "~/.copilot/skills/",
        "recommendation": "Prefer native Copilot OTel on OTLP HTTP; add hooks for governance.",
    },
    {
        "name": "OpenAI Codex CLI",
        "setup_aliases": ["codex", "codex-cli", "openai-codex"],
        "env": "CODEX_HOME",
        "default": lambda: Path.home() / ".codex",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "hook_agent": "codex",
        "global_path": "~/.agents/skills/",
        "recommendation": "Use native Codex OTel for interactive runs; reflect does not yet ship a native session adapter for Codex logs.",
    },
    {
        "name": "Windsurf",
        "setup_aliases": ["windsurf"],
        "env": "WINDSURF_HOME",
        "default": lambda: Path.home() / ".codeium" / "windsurf",
        "path_kind": "home",
        "local_skill_path": ".windsurf/skills/",
        "hook_agent": "windsurf",
        "global_path": "~/.codeium/windsurf/skills/",
        "recommendation": "Use opentelemetry-hooks for Windsurf telemetry; native OTel is not available.",
    },
    {
        "name": "Trae",
        "env": "TRAE_HOME",
        "default": lambda: Path.home() / ".trae",
        "path_kind": "home",
        "local_skill_path": ".trae/skills/",
        "global_path": "~/.trae/skills/",
        "recommendation": "Native OTel and hooks still need verification for Trae.",
    },
    {
        "name": "Cline",
        "env": "CLINE_HOME",
        "default": lambda: Path.home() / ".agents",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "global_path": "~/.agents/skills/",
        "recommendation": "Compatible with standard .agents/skills distribution.",
    },
    {
        "name": "Roo Code",
        "env": "ROO_HOME",
        "default": lambda: Path.home() / ".roo",
        "path_kind": "home",
        "local_skill_path": ".roo/skills/",
        "global_path": "~/.roo/skills/",
        "recommendation": "Native OTel and hooks still need verification for Roo Code.",
    },
    {
        "name": "Continue",
        "env": "CONTINUE_HOME",
        "default": lambda: Path.home() / ".continue",
        "path_kind": "home",
        "local_skill_path": ".continue/skills/",
        "global_path": "~/.continue/skills/",
        "recommendation": "Add hooks to cover exec / mcp-server gaps in Continue.",
    },
    {
        "name": "Goose",
        "env": "GOOSE_HOME",
        "default": lambda: Path.home() / ".config" / "goose",
        "path_kind": "home",
        "local_skill_path": ".goose/skills/",
        "global_path": "~/.config/goose/skills/",
        "recommendation": "Native OTel and hooks still need verification for Goose.",
    },
    {
        "name": "OpenHands",
        "env": "OPENHANDS_HOME",
        "default": lambda: Path.home() / ".openhands",
        "path_kind": "home",
        "local_skill_path": ".openhands/skills/",
        "global_path": "~/.openhands/skills/",
        "recommendation": "Native OTel and hooks still need verification for OpenHands.",
    },
    {
        "name": "Antigravity",
        "setup_aliases": ["agy", "antigravity-cli"],
        "env": "ANTIGRAVITY_HOME",
        "default": lambda: Path.home() / ".gemini" / "antigravity-cli",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "global_path": "~/.gemini/config/skills/",
        "recommendation": "Use agy with workspace MCP today; native telemetry ingestion still needs verification.",
    },
    {
        "name": "Amp",
        "env": "AMP_HOME",
        "default": lambda: Path.home() / ".local" / "share" / "amp",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "global_path": "~/.config/agents/skills/",
        "recommendation": "Start with session/log adapters before adding new default hook collection.",
    },
    {
        "name": "iFlow",
        "env": "IFLOW_HOME",
        "default": lambda: Path.home() / ".iflow",
        "path_kind": "home",
        "local_skill_path": ".iflow/skills/",
        "global_path": "~/.iflow/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "Pi",
        "env": "PI_HOME",
        "default": lambda: Path.home() / ".pi",
        "path_kind": "home",
        "local_skill_path": ".pi/skills/",
        "global_path": "~/.pi/agent/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "OpenClaw",
        "env": "OPENCLAW_HOME",
        "default": lambda: Path.home() / ".openclaw",
        "path_kind": "home",
        "local_skill_path": "skills/",
        "global_path": "~/.openclaw/skills/",
        "recommendation": "Start with session/log adapters; native OTel and hooks still need verification.",
    },
    {
        "name": "OpenCode",
        "env": "OPENCODE_HOME",
        "default": lambda: Path.home() / ".config" / "opencode",
        "path_kind": "home",
        "local_skill_path": ".opencode/skills/",
        "hook_agent": "opencode",
        "global_path": "~/.config/opencode/skills/",
        "recommendation": "Use opencode run for skill extraction; Reflect reads the native OpenCode session store.",
    },
]

_SKILL_AGENT_CLI_NAMES = ("claude", "codex", "copilot", "cursor-agent", "agy", "opencode")


def _complete_setup_agent(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[str]:
    """Complete supported setup display names and stable aliases."""
    candidates = {
        value
        for spec in _AGENT_SPECS
        for value in (str(spec["name"]), *map(str, spec.get("setup_aliases", [])))
    }
    lowered = incomplete.lower()
    return sorted(value for value in candidates if value.lower().startswith(lowered))


def _complete_skill_agent_cli(
    _ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[str]:
    """Complete supported coding-agent executables used for skill authoring."""
    lowered = incomplete.lower()
    return [name for name in _SKILL_AGENT_CLI_NAMES if name.startswith(lowered)]

_IMPLEMENTED_TELEMETRY_SUPPORT: dict[str, tuple[str, str]] = {
    "Claude Code": ("Native OTel + hooks", "High"),
    "Cursor": ("Session/log adapters", "Medium"),
    "Gemini CLI": ("Native OTel + session adapters", "High"),
    "GitHub Copilot": ("Native OTel + VS Code env", "High"),
    "OpenAI Codex CLI": ("Native OTel config", "Medium"),
    "Windsurf": ("Hook telemetry + config snapshots", "Medium"),
    "OpenCode": ("Native SQLite session adapter + hooks", "Medium"),
}
_DOCTOR_MATRIX_PLANNED = {"Antigravity", "OpenClaw"}


def _agent_support_summary(name: str) -> dict[str, str]:
    telemetry_path, confidence = _IMPLEMENTED_TELEMETRY_SUPPORT.get(
        name,
        ("Not implemented yet (setup only snapshots skills/config)", "Planned"),
    )
    status = "Implemented" if name in _IMPLEMENTED_TELEMETRY_SUPPORT else "Planned"
    mcp_client = get_mcp_client_capability(name)
    return {
        "support_status": status,
        "telemetry_path": telemetry_path,
        "mcp_client": mcp_client.surface.value if mcp_client else "Not declared",
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


def _detect_skill_drift(agents: list[dict]) -> dict | None:
    reflect_source = _bundled_reflect_skill_dir()
    if reflect_source is None:
        return None

    bundled_root = Path(__file__).parent / "data" / "skills"
    source_dirs = {
        "reflect": reflect_source,
        **{
            name: bundled_root / name
            for name in ("reflect-skills", "reflect-usage")
            if (bundled_root / name / "SKILL.md").exists()
        },
    }
    source_signatures = {
        name: signature
        for name, source_dir in source_dirs.items()
        if (signature := _directory_signature(source_dir))
    }
    if not source_signatures:
        return None

    drifted_targets: list[str] = []
    missing_targets: list[str] = []
    for agent in agents:
        if not agent.get("detected"):
            continue
        global_root = Path(agent["global_path"]).expanduser()
        for skill_name, source_signature in source_signatures.items():
            global_skill = global_root / skill_name
            target = f"{agent['name']}/{skill_name}"
            if not global_skill.exists():
                missing_targets.append(target)
            elif _directory_signature(global_skill) != source_signature:
                drifted_targets.append(target)

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
        "remediation": "Run reflect setup to refresh global installed skill copies.",
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
    if HookRuntime.discover() is None:
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
    help="Also save a markdown report to this file.",
)
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file from the collector file exporter.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used for SQL-backed browser report endpoints.",
)
@click.option(
    "--demo",
    is_flag=True,
    help="Run with bundled sample data. Great for first-time users or screenshots.",
)
@click.option("--foreground", is_flag=True, help="Keep the browser report server attached to this terminal.")
@click.option("--period", "time_range", type=click.Choice(["day", "week", "month", "all"]), default="week", show_default=True)
@click.pass_context
def main(
    ctx: click.Context,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    output: Path | None,
    otlp_traces: Path | None,
    db_path: Path,
    demo: bool,
    foreground: bool,
    time_range: str,
) -> None:
    """Open the local Reflect browser report."""
    if ctx.invoked_subcommand is not None:
        return

    if not foreground and output is None and not demo:
        _start_background_report_server(
            db_path=db_path,
            otlp_traces=otlp_traces,
            refresh=True,
        )
        return

    _run_browser_report(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        time_range=time_range,
        demo=demo,
        output=output,
        db_path=db_path,
        refresh=True,
    )


@main.command("completion")
@click.option(
    "--shell",
    type=click.Choice(SUPPORTED_SHELLS, case_sensitive=False),
    default=None,
    help="Shell to target. Defaults to the current $SHELL.",
)
@click.option(
    "--install",
    is_flag=True,
    help="Install and activate completion for the selected shell.",
)
def completion(shell: str | None, install: bool) -> None:
    """Generate or install autocomplete for every Reflect command."""
    manager = ShellCompletionManager(main)
    selected_shell = shell.lower() if shell else manager.detect_shell()
    if selected_shell is None:
        supported = ", ".join(SUPPORTED_SHELLS)
        raise click.ClickException(
            f"Could not detect a supported shell from $SHELL; pass --shell ({supported})."
        )
    if not install:
        click.echo(manager.source(selected_shell), nl=False)
        return
    result = manager.install(selected_shell)
    state = "Installed" if result.changed else "Already current"
    click.echo(f"{state}: {result.script_path}")
    if result.config_path is not None:
        click.echo(f"Activated from: {result.config_path}")
    click.echo("Restart the shell or source its configuration to enable autocomplete.")


def _demo_otlp_traces() -> Path:
    bundled = Path(__file__).parent / "data" / "demo-traces.json"
    if not bundled.exists():
        bundled = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
    if not bundled.exists():
        raise click.ClickException("Demo data was not found; reinstall Reflect or run from the repository root")
    return bundled


def _open_improvement_service(
    db_path: Path,
    *,
    read_only: bool = False,
) -> tuple[sqlite3.Connection, ImprovementService]:
    from reflect.improvements.service import ImprovementService
    from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only

    conn = (
        connect_sqlite_read_only(db_path)
        if read_only
        else connect_sqlite(db_path)
    )
    return conn, ImprovementService(conn, initialize_schema=not read_only)


def _improvement_snapshot_status(db_path: Path) -> SnapshotStatus:
    from reflect.preparation import SQLiteSnapshotInspector

    return SQLiteSnapshotInspector(db_path).inspect()


def _prepare_sql_snapshot_with_progress(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool,
    spans_dir: Path | None = None,
) -> dict[str, object]:
    from reflect.terminal import TerminalPreparationProgress

    return TerminalPreparationProgress().run(
        lambda progress: _prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=include_native_sessions,
            spans_dir=spans_dir,
            progress=progress,
        )
    )


def _ensure_command_snapshot(
    db_path: Path,
    *,
    refresh: bool | None,
    otlp_traces: Path | None = None,
    include_native_sessions: bool = True,
    spans_dir: Path | None = None,
    prepare: Callable[[], dict[str, object]] | None = None,
    readiness_probes: tuple[SnapshotReadinessProbe, ...] = (),
) -> SnapshotPreparationResult:
    from reflect.preparation import (
        CallableSnapshotRefresher,
        SnapshotLifecycleService,
        SnapshotUnavailableError,
        SQLiteSnapshotInspector,
    )

    refresh_operation = prepare or (
        lambda: _prepare_sql_snapshot_with_progress(
            db_path,
            otlp_traces=(
                otlp_traces
                if otlp_traces is not None
                else _default_otlp_traces()
            ),
            include_native_sessions=include_native_sessions,
            spans_dir=spans_dir,
        )
    )
    lifecycle = SnapshotLifecycleService(
        SQLiteSnapshotInspector(
            db_path,
            readiness_probes=readiness_probes,
        ),
        refresher=CallableSnapshotRefresher(refresh_operation),
        refresh_hint=(
            f"Run `reflect refresh --db-path {db_path}` or re-run this command "
            "with `--refresh`."
        ),
    )
    try:
        return lifecycle.prepare(requested_refresh=refresh)
    except SnapshotUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc


def _require_snapshot_schema(db_path: Path, *, refresh_hint: str) -> None:
    from reflect.preparation import (
        CommandPreparationPolicy,
        SnapshotLifecycleService,
        SnapshotUnavailableError,
        SQLiteSnapshotInspector,
    )

    lifecycle = SnapshotLifecycleService(
        SQLiteSnapshotInspector(db_path),
        policy=CommandPreparationPolicy(require_sessions=False),
        refresh_hint=refresh_hint,
    )
    try:
        lifecycle.prepare(requested_refresh=None)
    except SnapshotUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc


def _format_usage_duration(duration_ms: int) -> str:
    total_seconds = max(duration_ms, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _render_usage_report(console: Console, report) -> None:
    totals = report.totals
    if report.session is not None:
        title = f"Usage · {report.session.agent} · {report.session.id}"
        context = report.session.workspace or report.session.repository or report.session.title or "Local session"
    else:
        title = f"Global usage · {report.period}"
        context = "All matching local sessions"

    summary = Table.grid(expand=True, padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right", style="orange3")
    summary.add_column(style="bold")
    summary.add_column(justify="right", style="orange3")
    summary.add_row("Sessions", f"{totals.sessions:,}", "Prompts", f"{totals.prompts:,}")
    summary.add_row("LLM calls", f"{totals.llm_calls:,}", "Tool calls", f"{totals.tool_calls:,}")
    summary.add_row("MCP calls", f"{totals.mcp_calls:,}", "Subagents", f"{totals.subagent_launches:,}")
    summary.add_row("Failures", f"{totals.failures:,}", "Recovered", f"{totals.recovered_failures:,}")
    summary.add_row("Duration", _format_usage_duration(totals.duration_ms), "Estimated cost", f"${totals.estimated_cost_usd:,.2f}")
    console.print(Panel(summary, title=title, subtitle=context, border_style="orange3"))

    tokens = Table(title="Tokens", border_style="grey37")
    tokens.add_column("Input", justify="right")
    tokens.add_column("Output", justify="right")
    tokens.add_column("Cache write", justify="right")
    tokens.add_column("Cache read", justify="right")
    tokens.add_column("Reasoning", justify="right")
    tokens.add_row(
        f"{totals.input_tokens:,}",
        f"{totals.output_tokens:,}",
        f"{totals.cache_creation_tokens:,}",
        f"{totals.cache_read_tokens:,}",
        f"{totals.reasoning_tokens:,}",
    )
    console.print(tokens)
    provenance = report.token_provenance
    console.print(
        "[dim]Token provenance: "
        f"{provenance.exact_sessions} exact · "
        f"{provenance.estimated_sessions} estimated · "
        f"{provenance.unavailable_sessions} unavailable[/dim]"
    )

    for heading, entries in (("Models", report.models), ("Tools", report.tools), ("Agents", report.agents)):
        if not entries:
            continue
        table = Table(title=heading, border_style="grey37")
        table.add_column(heading[:-1])
        table.add_column("Uses", justify="right")
        if heading != "Tools":
            table.add_column("Tokens", justify="right")
            table.add_column("Cost", justify="right")
        for entry in entries:
            row = [entry.name, f"{entry.count:,}"]
            if heading != "Tools":
                row.extend(
                    [
                        f"{entry.input_tokens + entry.output_tokens:,}",
                        f"${entry.estimated_cost_usd:,.2f}",
                    ]
                )
            table.add_row(*row)
        console.print(table)

    for limitation in report.limitations:
        console.print(f"[yellow]Note:[/yellow] {limitation}")


def _prepare_usage_db(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool,
    native_session_ids: tuple[str, ...] = (),
    progress: PreparationProgressReporter | None = None,
) -> dict[str, object]:
    """Refresh usage facts without rebuilding graph or improvement state."""
    from reflect.preparation import PreparationStage, report_preparation_progress
    from reflect.store.cursor_adapter import (
        apply_cursor_transcript_usage_estimates,
        repair_misattributed_cursor_transcript_usage,
    )
    from reflect.store.ingest import (
        ingest_codex_context_file,
        ingest_native_session_file,
        ingest_otlp_logs_file,
        ingest_otlp_traces_file,
    )
    from reflect.store.migrate import migrate
    from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
    from reflect.store.rollups import rebuild_rollups, refresh_rollups, rollup_rebuild_pending
    from reflect.store.sqlite import connect_sqlite
    from reflect.store.workspaces import backfill_session_context

    report_preparation_progress(
        progress,
        PreparationStage.OPENING_STORE,
        "Opening the local telemetry store...",
    )
    conn = connect_sqlite(db_path)
    try:
        report_preparation_progress(
            progress,
            PreparationStage.MIGRATING_SCHEMA,
            "Checking database migrations...",
        )
        applied = migrate(conn)
        if otlp_traces is not None and otlp_traces.exists():
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new OTLP traces...",
            )
            ingest_otlp_traces_file(conn, file_path=otlp_traces, skip_unchanged=True)
            otlp_logs = _infer_otlp_logs_file(otlp_traces)
            if otlp_logs is not None and otlp_logs.exists():
                report_preparation_progress(
                    progress,
                    PreparationStage.INGESTING_LOGS,
                    "Reading new OTLP logs...",
                )
                ingest_otlp_logs_file(conn, file_path=otlp_logs, skip_unchanged=True)

        changed_session_ids: set[str] = set()
        if conn.execute(
            "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
        ).fetchone():
            report_preparation_progress(
                progress,
                PreparationStage.NORMALIZING,
                "Normalizing usage telemetry...",
            )
            normalize_pending_raw_events(conn, changed_session_ids=changed_session_ids)

        cursor_native_files: list[Path] = []
        if include_native_sessions:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_SESSIONS,
                "Reading local agent sessions...",
            )
            for native_agent, session_file in _discover_rich_session_files():
                if native_session_ids and native_agent != "opencode" and not any(
                    _native_session_path_matches_id(session_file, candidate)
                    for candidate in native_session_ids
                ):
                    continue
                if native_agent == "codex":
                    ingest_codex_context_file(conn, file_path=session_file)
                result = ingest_native_session_file(
                    conn,
                    file_path=session_file,
                    agent=native_agent,
                    source_id=f"native_session:{native_agent}:{session_file}",
                    skip_existing_sessions=native_agent != "opencode",
                    skip_unchanged=True,
                )
                if native_agent == "cursor" and not result.get("unchanged"):
                    cursor_native_files.append(session_file)
            report_preparation_progress(
                progress,
                PreparationStage.NORMALIZING,
                "Normalizing usage telemetry...",
            )
            normalize_pending_raw_events(conn, changed_session_ids=changed_session_ids)
        report_preparation_progress(
            progress,
            PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating canonical usage state...",
        )
        backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
            changed_session_ids=changed_session_ids,
        )
        cursor_provenance_result = repair_misattributed_cursor_transcript_usage(conn)
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_provenance_result["session_ids"]
            if session_id
        )
        cursor_result = apply_cursor_transcript_usage_estimates(conn, cursor_native_files)
        context_result = backfill_session_context(
            conn,
            timestamp=datetime.now(UTC).isoformat(),
            changed_session_ids=changed_session_ids,
        )
        session_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        rollup_count = int(conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0])
        from reflect.store.cost_refresh import CostRefreshState

        cost_state = CostRefreshState(conn)
        cost_inputs = cost_state.inspect(session_ids=changed_session_ids) if session_count else None
        if (
            rollup_rebuild_pending(conn)
            or session_count != rollup_count
            or cursor_result.get("updated")
            or (cost_inputs is not None and cost_inputs.requires_full_refresh)
        ):
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                "Rebuilding usage rollups...",
            )
            _ensure_sql_costs(conn, refresh_inputs=cost_inputs)
            rebuild_rollups(conn)
            if cost_inputs is not None and cost_inputs.requires_full_refresh:
                cost_state.mark_current(cost_inputs)
        elif changed_session_ids or context_result["sessions_updated"]:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Refreshing usage rollups for {len(changed_session_ids):,} session(s)...",
            )
            _ensure_sql_costs(
                conn,
                session_ids=changed_session_ids,
                refresh_inputs=cost_inputs,
            )
            refresh_rollups(conn, changed_session_ids)
        report_preparation_progress(
            progress,
            PreparationStage.COMPLETE,
            "Usage refresh complete.",
        )
        return {
            "refreshed": True,
            "changed_sessions": len(changed_session_ids),
        }
    finally:
        conn.close()


def _prepare_usage_db_with_progress(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool,
    native_session_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    from reflect.terminal import TerminalPreparationProgress

    return TerminalPreparationProgress().run(
        lambda progress: _prepare_usage_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=include_native_sessions,
            native_session_ids=native_session_ids,
            progress=progress,
        )
    )


@main.command("refresh")
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file. Defaults to the configured local trace file.",
)
@click.option(
    "--native-sessions/--no-native-sessions",
    default=True,
    help="Include discoverable native agent session stores.",
)
@click.option(
    "--spans-dir",
    type=click.Path(path_type=Path),
    default=None,
    help="Hook JSONL directory. Defaults to the configured local spans directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the refresh result as JSON.")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite telemetry store.",
)
def refresh_snapshot(
    otlp_traces: Path | None,
    native_sessions: bool,
    spans_dir: Path | None,
    as_json: bool,
    db_path: Path,
) -> None:
    """Explicitly ingest telemetry and rebuild the prepared local snapshot."""
    if spans_dir is not None and not spans_dir.is_dir():
        raise click.ClickException(f"Hook spans directory not found: {spans_dir}")
    result = _ensure_command_snapshot(
        db_path,
        refresh=True,
        otlp_traces=otlp_traces,
        include_native_sessions=native_sessions,
        spans_dir=spans_dir or _default_spans_dir(),
    )
    payload = result.refresh_result or {}
    if as_json:
        _echo_json(payload)
        return
    click.echo(
        "Refreshed local snapshot "
        f"(sessions={payload.get('sessions', 'unknown')}, db={db_path})"
    )


@main.command("usage")
@click.option("--session", "session_id", shell_complete=complete_session_id, help="Inspect one session ID.")
@click.option("--global", "global_scope", is_flag=True, help="Aggregate all matching local sessions.")
@click.option("--agent", help="Limit global usage to one agent name or ID.")
@click.option(
    "--period",
    type=click.Choice(["day", "week", "month", "all"]),
    help="Evidence period. Defaults to week.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Ingest local telemetry before reporting. Slower on large native session stores.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the usage report as JSON.")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite telemetry store.",
)
def usage(
    session_id: str | None,
    global_scope: bool,
    agent: str | None,
    period: str | None,
    refresh: bool,
    as_json: bool,
    db_path: Path,
) -> None:
    """Show exact local usage for the current session or a global period."""
    from reflect.store.rollups import UsageRollupReadinessProbe
    from reflect.store.sqlite import connect_sqlite_read_only
    from reflect.usage import UsageService

    if session_id and global_scope:
        raise click.UsageError("--session and --global cannot be used together")
    if agent and not global_scope:
        raise click.UsageError("--agent is only valid with --global")
    selected_period = period or "week"

    runtime_hints = UsageService.environment_session_hints()
    _ensure_command_snapshot(
        db_path,
        refresh=refresh,
        readiness_probes=(
            UsageRollupReadinessProbe(
                session_ids=(session_id,) if session_id and not global_scope else None
            ),
        ),
        prepare=lambda: _prepare_usage_db_with_progress(
            db_path,
            otlp_traces=_default_otlp_traces(),
            include_native_sessions=True,
            native_session_ids=(
                ()
                if global_scope
                else (session_id,) if session_id
                else tuple(hint.session_id for hint in runtime_hints)
            ),
        ),
    )

    conn = connect_sqlite_read_only(db_path)
    try:
        report = UsageService(conn).report(
            session_id=session_id,
            global_scope=global_scope,
            period=selected_period,
            agent=agent,
        )
    except (LookupError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if as_json:
        _echo_json(report.model_dump(mode="json"))
        return
    _render_usage_report(Console(), report)


@main.command("improve")
@click.argument("observation_id", required=False, shell_complete=complete_observation_id)
@click.option("--demo", is_flag=True, help="Use bundled sample telemetry in an isolated database.")
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option(
    "--path",
    "scope_path",
    type=click.Path(path_type=Path, exists=True),
    help="Use prior evidence for the matching repository or workspace.",
)
@click.option(
    "--session",
    "session_id",
    help="Use one session and its child-agent sessions. Pass 'current' for the active runtime.",
)
@click.option("--global", "global_scope", is_flag=True, help="Use all local sessions in one explicit time range.")
@click.option(
    "--period",
    type=click.Choice(["day", "week", "month", "all"]),
    help="Global evidence period.",
)
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh local telemetry before reading; the default reads the existing snapshot.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite improvement ledger.",
)
def improve(
    observation_id: str | None,
    demo: bool,
    as_json: bool,
    scope_path: Path | None,
    session_id: str | None,
    global_scope: bool,
    period: str | None,
    refresh: bool | None,
    db_path: Path,
) -> None:
    """Show scoped actionable improvements or inspect OBSERVATION_ID."""
    import tempfile

    from reflect.improvements.scope import ImprovementScopeResolver

    selectors = int(scope_path is not None) + int(session_id is not None) + int(global_scope)
    if selectors > 1:
        raise click.UsageError("--path, --session, and --global are mutually exclusive")
    selected_period = period
    if selected_period and not global_scope:
        raise click.UsageError("--period requires --global")
    if global_scope and selected_period is None:
        raise click.UsageError("--global requires --period day|week|month|all")

    temporary: tempfile.TemporaryDirectory[str] | None = None
    if demo:
        temporary = tempfile.TemporaryDirectory(prefix="reflect-improve-demo-")
        db_path = Path(temporary.name) / "reflect.db"
        refresh = True
    try:
        _ensure_command_snapshot(
            db_path,
            refresh=refresh,
            otlp_traces=_demo_otlp_traces() if demo else _default_otlp_traces(),
            include_native_sessions=not demo,
        )
        conn, service = _open_improvement_service(db_path, read_only=True)
        try:
            resolver = ImprovementScopeResolver(conn)
            explicit_scope = bool(scope_path is not None or session_id is not None or global_scope)
            if global_scope:
                scope = resolver.global_period(selected_period or "week")
            elif session_id:
                scope = resolver.session(session_id)
            elif observation_id and not explicit_scope:
                scope = None
            elif demo:
                scope = resolver.global_period("all")
            else:
                scope = resolver.path(scope_path)
            result = service.improve(observation_id, refresh=False, scope=scope)
            ledger = (
                service.finding_session_ledger(observation_id, scope=scope, limit=50)
                if observation_id
                else None
            )
            if as_json:
                if ledger is not None:
                    payload = result.model_dump(mode="json")
                    payload["session_ledger"] = ledger.model_dump(mode="json")
                    _echo_json(payload)
                else:
                    _echo_json(result.model_dump(mode="json"))
                return
            console = Console(force_terminal=True)
            if observation_id:
                _render_improvement_detail(console, result, ledger=ledger)
                return
            if not result.observations:
                label = result.resolved_scope.label if result.resolved_scope else "the selected scope"
                console.print(
                    f"[green]No actionable recurring observations found for {label}.[/green]"
                )
                if result.resolved_scope and not result.resolved_scope.matched:
                    console.print(
                        "[dim]Reflect did not fall back to global data. "
                        "Use --global --period day|week|month|all.[/dim]"
                    )
                if not result.attribution_complete:
                    console.print(
                        "[yellow]Some legacy findings still have capped source-session "
                        "attribution. Run with --refresh to rebuild supported detectors.[/yellow]"
                    )
                return
            scope_label = result.resolved_scope.label if result.resolved_scope else "local"
            table = Table(
                title=f"Observed Improvements · {scope_label}",
                border_style="orange3",
            )
            table.add_column("ID", style="cyan", no_wrap=True)
            table.add_column("Finding")
            table.add_column("Impact", justify="right")
            table.add_column("Sessions", justify="right")
            table.add_column("Latest")
            table.add_column("State")
            for observation in result.observations[:10]:
                affected = (
                    observation.scope_affected_session_count
                    if observation.scope_affected_session_count is not None
                    else observation.affected_session_count
                )
                eligible = observation.eligible_session_count
                table.add_row(
                    observation.id,
                    observation.title,
                    f"{observation.impact_score:.0f}",
                    f"{affected}/{eligible}" if eligible is not None else str(affected),
                    (observation.latest_source_at or observation.last_seen_at)[:10],
                    observation.status.value.replace("_", " "),
                )
            console.print(table)
            if not result.attribution_complete:
                console.print(
                    "[yellow]Some legacy findings still have capped source-session "
                    "attribution. Run with --refresh to rebuild supported detectors.[/yellow]"
                )
            console.print(
                f"[dim]{result.pending_workflows} pending workflow(s). "
                "Inspect one with: reflect improve <ID>[/dim]"
            )
        finally:
            conn.close()
    finally:
        if temporary is not None:
            temporary.cleanup()


def _render_improvement_detail(console: Console, observation, *, ledger=None) -> None:
    body = [
        f"[bold]{observation.title}[/bold]",
        observation.summary,
        "",
        f"Impact: {observation.impact_score:.0f}/100  •  Severity: {observation.severity.value}",
        (
            f"Confidence: {observation.confidence:.0%}  •  Sessions: "
            f"{observation.scope_affected_session_count if observation.scope_affected_session_count is not None else observation.affected_session_count}"
        ),
        f"Rule: {observation.rule_id} v{observation.rule_version}",
    ]
    if observation.resolved_scope:
        body.extend(
            [
                f"Scope: {observation.resolved_scope.label}",
                f"Latest source evidence: {observation.latest_source_at or 'unknown'}",
            ]
        )
    if observation.candidate_id:
        body.extend(
            [
                "",
                f"Proposed workflow: {observation.candidate_id} ({observation.candidate_status.value})",
                f"Review: reflect workflows show {observation.candidate_id}",
            ]
        )
    console.print(Panel("\n".join(body), title=observation.id, border_style="orange3"))
    if observation.evidence:
        evidence = Table(title="Evidence", border_style="dim")
        evidence.add_column("Entity")
        evidence.add_column("Summary")
        for item in observation.evidence[:20]:
            evidence.add_row(f"{item.entity_type}:{item.entity_id}", item.summary_redacted)
        console.print(evidence)
    if ledger is not None and ledger.source_sessions:
        sessions = Table(title="Source Sessions", border_style="dim")
        sessions.add_column("Session", style="cyan")
        sessions.add_column("Agent")
        sessions.add_column("Workspace")
        sessions.add_column("Started")
        sessions.add_column("Evidence", justify="right")
        sessions.add_column("Why")
        for item in ledger.source_sessions:
            sessions.add_row(
                item.session_id,
                item.agent or "unknown",
                item.workspace or "unknown",
                item.started_at[:19],
                str(item.evidence_count),
                item.evidence_summaries[0] if item.evidence_summaries else "",
            )
        console.print(sessions)
        console.print(
            "[dim]Inspect one run with: reflect usage --session <SESSION_ID> --json[/dim]"
        )


@main.command("ask")
@click.argument("question")
@click.option("--json", "as_json", is_flag=True, help="Print the answer packet as JSON.")
@click.option("--task-file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--path", "context_path", type=click.Path(path_type=Path, exists=True))
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh telemetry first; the default reads an existing snapshot.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite improvement ledger.",
)
def ask(
    question: str,
    as_json: bool,
    task_file: Path | None,
    context_path: Path | None,
    refresh: bool | None,
    db_path: Path,
) -> None:
    """Answer QUESTION from reviewed workflows, linked evidence, and scoped memory."""
    _ensure_command_snapshot(db_path, refresh=refresh)
    from reflect.context import ReflectContextService
    from reflect.store.sqlite import connect_sqlite_read_only

    conn = connect_sqlite_read_only(db_path)
    try:
        service = ReflectContextService(conn, initialize_schema=False)
        answer = service.ask(question, task_file=task_file, path=context_path)
    finally:
        conn.close()
    if as_json:
        _echo_json(answer.model_dump(mode="json"))
        return
    click.echo(answer.answer)
    if answer.guidance:
        click.echo("\nGuidance")
        for index, step in enumerate(answer.guidance, start=1):
            click.echo(f"  {index}. {step}")
    if answer.evidence:
        click.echo("\nEvidence")
        for item in answer.evidence:
            click.echo(f"  {item.kind}:{item.id} — {item.summary}")
    if answer.memories:
        click.echo("\nMemory Context")
        for item in answer.memories:
            status = item.validation_status or item.provenance
            click.echo(f"  {item.provider}:{item.id} [{status}] — {item.content}")
    if answer.constraints:
        click.echo("\nConstraints")
        for item in answer.constraints:
            click.echo(f"  - {item}")
    if answer.verification:
        click.echo("\nVerification")
        for item in answer.verification:
            click.echo(f"  - {item}")
    if answer.fallback:
        click.echo(f"\nFallback: {answer.fallback}")
    for limitation in answer.limitations:
        click.echo(f"\nNote: {limitation}")


@main.group("workflows")
def workflows() -> None:
    """Review and manage durable workflow proposals."""


_WORKFLOW_BEHAVIOR_TYPES = ("loop", "recovery", "verification", "exploration", "proven_pattern")
_WORKFLOW_STATUSES = ("pending", "approved", "active", "stale", "rejected", "rolled_back")


def _print_workflow_table(candidates, *, title: str = "Workflows") -> None:
    table = Table(title=title, border_style="orange3")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Behavior")
    table.add_column("Workflow")
    table.add_column("Origin")
    table.add_column("Support", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("State")
    for candidate in candidates:
        content = candidate.content or {}
        source = content.get("source") or {}
        source_kind = str(source.get("kind") or candidate.provenance.get("source") or "")
        if source_kind in {"agent_authored", "skill_extraction"}:
            origin = f"agent draft ({source.get('agent')})" if source.get("agent") else "agent draft"
        elif source_kind == "manual_skill_file":
            origin = "imported skill"
        else:
            origin = "rule blueprint"
        table.add_row(
            candidate.id,
            str(content.get("behavior_type") or "proven_pattern").replace("_", " "),
            str(content.get("slug") or candidate.title),
            origin,
            str(candidate.support_count),
            f"{candidate.confidence:.0%}",
            candidate.status.value,
        )
    Console(force_terminal=True).print(table)


@workflows.command("list")
@click.option("--json", "as_json", is_flag=True)
@click.option("--type", "behavior_type", type=click.Choice(_WORKFLOW_BEHAVIOR_TYPES))
@click.option("--status", type=click.Choice(_WORKFLOW_STATUSES))
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh telemetry first; the default reads an existing snapshot.",
)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def workflows_list(
    as_json: bool,
    behavior_type: str | None,
    status: str | None,
    refresh: bool | None,
    db_path: Path,
) -> None:
    """List pending, active, and rolled-back workflows."""
    if refresh is True:
        _ensure_command_snapshot(db_path, refresh=True)
    else:
        _require_snapshot_schema(
            db_path,
            refresh_hint=(
                f"Run `reflect refresh --db-path {db_path}` to prepare telemetry "
                "or `reflect workflows add SKILL.md` to create a manual workflow."
            ),
        )
    conn, service = _open_improvement_service(db_path, read_only=True)
    try:
        candidates = service.workflows.list(
            behavior_types={behavior_type} if behavior_type else None,
            statuses={status} if status else None,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json([candidate.model_dump(mode="json") for candidate in candidates])
        return
    _print_workflow_table(candidates)


def _read_workflow_skill(path: Path, behavior_type: str) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise click.ClickException("SKILL.md must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise click.ClickException("SKILL.md frontmatter is missing its closing --- line")
    metadata: dict[str, str] = {}
    for line in text[4:end].splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() in {"name", "description"}:
            metadata[key.strip()] = value.strip().strip("'\"")
    try:
        name = _validate_skill_name(metadata.get("name"))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    description = metadata.get("description", "").strip()
    if not description:
        raise click.ClickException("SKILL.md frontmatter requires a description")
    source_markdown = text[end + 5:].strip()
    if not source_markdown:
        raise click.ClickException("SKILL.md requires workflow instructions after frontmatter")
    return {
        "name": name,
        "description": description,
        "content": source_markdown,
        "behavior_type": behavior_type,
        "source_kind": "manual_skill_file",
    }


@workflows.command("add")
@click.argument("skill_file", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--type", "behavior_type", type=click.Choice(_WORKFLOW_BEHAVIOR_TYPES), default="proven_pattern", show_default=True)
@click.option(
    "--source-agent",
    metavar="NAME",
    help="Record NAME as the coding agent that authored this draft.",
    shell_complete=_complete_skill_agent_cli,
)
@click.option(
    "--from-workflow",
    "source_workflow_id",
    metavar="ID",
    help="Link the draft to an existing workflow's bounded source-session evidence.",
    shell_complete=complete_workflow_id,
)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def workflows_add(
    skill_file: Path,
    behavior_type: str,
    source_agent: str | None,
    source_workflow_id: str | None,
    db_path: Path,
) -> None:
    """Stage an existing SKILL.md as a pending workflow; do not install it."""
    skill = _read_workflow_skill(skill_file, behavior_type)
    normalized_agent = " ".join((source_agent or "").split())[:80]
    if normalized_agent:
        skill["source_kind"] = "agent_authored"
    if source_workflow_id:
        skill["source_workflow_id"] = source_workflow_id
    conn, service = _open_improvement_service(db_path)
    try:
        source_session_ids: list[str] = []
        if source_workflow_id:
            try:
                source_ledger = service.repository.workflow_session_ledger(
                    source_workflow_id,
                    limit=200,
                )
            except KeyError as exc:
                raise click.ClickException(str(exc)) from exc
            source_session_ids = [item.session_id for item in source_ledger.source_sessions]
        candidate_id = service.stage_extracted_skills(
            [skill],
            session_ids=source_session_ids,
            source_agent=normalized_agent or None,
        )[0]
    finally:
        conn.close()
    click.echo(f"Staged {candidate_id} from {skill_file}")
    if normalized_agent:
        click.echo(f"Authorship: agent draft ({normalized_agent})")
    if source_workflow_id:
        click.echo(
            f"Evidence: {len(source_session_ids)} source session(s) from {source_workflow_id}"
        )
    click.echo(f"Review: reflect workflows show {candidate_id}")
    click.echo("Nothing was installed. Apply it from the target Git repository after review.")


@workflows.command("show")
@click.argument("candidate_id", shell_complete=complete_workflow_id)
@click.option("--json", "as_json", is_flag=True)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def workflows_show(candidate_id: str, as_json: bool, db_path: Path) -> None:
    """Show a workflow proposal and its review state."""
    _require_snapshot_schema(
        db_path,
        refresh_hint=(
            f"Run `reflect refresh --db-path {db_path}` to prepare the workflow ledger."
        ),
    )
    conn, service = _open_improvement_service(db_path, read_only=True)
    try:
        candidate = service.workflows.show(candidate_id)
        preview = service.workflows.preview(candidate_id, project_root=Path.cwd())
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if as_json:
        payload = candidate.model_dump(mode="json")
        payload["preview"] = preview
        _echo_json(payload)
        return
    console = Console(force_terminal=True)
    steps = "\n".join(
        f"{index}. {step}" for index, step in enumerate(candidate.content.get("steps", []), start=1)
    )
    console.print(
        Panel(
            f"[bold]{candidate.title}[/bold]\n{candidate.hypothesis}\n\n"
            f"State: {candidate.status.value}  •  Risk: {candidate.risk}  •  "
            f"Support: {candidate.support_count}\n\n{steps}\n\n"
            f"Target: {candidate.target_metric} over {candidate.measurement_window} comparable sessions",
            title=candidate.id,
            border_style="orange3",
        )
    )
    console.print(
        Panel(
            preview["diff"] or "No filesystem change: the rendered workflow already matches.",
            title=f"Exact diff · {preview['target_path']}",
            border_style="dim",
        )
    )


@workflows.command("apply")
@click.argument("candidate_id", shell_complete=complete_workflow_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def workflows_apply(candidate_id: str, db_path: Path) -> None:
    """Approve and apply CANDIDATE_ID as a repo-local skill."""
    conn, service = _open_improvement_service(db_path)
    try:
        result = service.workflows.apply(candidate_id, project_root=Path.cwd())
    except (KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    action = "Already active" if result.get("idempotent") else "Applied"
    click.echo(f"{action} {candidate_id} at {result['target_path']}")
    click.echo(f"Rollback: reflect workflows rollback {candidate_id}")


@workflows.command("rollback")
@click.argument("candidate_id", shell_complete=complete_workflow_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def workflows_rollback(candidate_id: str, db_path: Path) -> None:
    """Roll back the active application of CANDIDATE_ID."""
    conn, service = _open_improvement_service(db_path)
    try:
        result = service.workflows.rollback(candidate_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Rolled back {candidate_id} at {result['target_path']}")


@main.group("loops", invoke_without_command=True)
@click.option("--json", "as_json", is_flag=True)
@click.option("--kind", type=click.Choice(["agent_native", "stalled", "productive"]))
@click.option(
    "--status",
    type=click.Choice(["detected", "acknowledged", "promoted", "dismissed", "resolved"]),
)
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh loop detection first; the default reads the existing loop registry.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
@click.pass_context
def loops(
    ctx: click.Context,
    as_json: bool,
    kind: str | None,
    status: str | None,
    refresh: bool | None,
    db_path: Path,
) -> None:
    """Inspect agent-native continuations and observed behavioral loops."""
    if ctx.invoked_subcommand:
        return
    from reflect.improvements.models import LoopKind, LoopStatus

    preparation = _ensure_command_snapshot(db_path, refresh=refresh)
    conn, improvement_service = _open_improvement_service(db_path, read_only=True)
    try:
        records = improvement_service.loops.list(
            kind=LoopKind(kind) if kind else None,
            status=LoopStatus(status) if status else None,
            limit=500,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json(
            {
                "refresh": {"prepared": 1} if preparation.refreshed else None,
                "prepared": preparation.refreshed,
                "loops": [item.model_dump(mode="json") for item in records],
            }
        )
        return
    _print_loops(
        records,
        refresh={"prepared": 1} if preparation.refreshed else None,
    )


def _print_loops(records, *, refresh: dict[str, int] | None = None) -> None:
    table = Table(title="Observed Loops", border_style="orange3")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Kind")
    table.add_column("Loop")
    table.add_column("Sessions", justify="right")
    table.add_column("Occurrences", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("State")
    for item in records:
        table.add_row(
            item.id,
            item.kind.value,
            item.title,
            str(item.affected_session_count),
            str(item.occurrence_count),
            f"{item.confidence:.0%}",
            item.status.value,
        )
    Console(force_terminal=True).print(table)
    if refresh:
        if refresh.get("prepared"):
            click.echo("Refreshed telemetry and the loop registry.")
            return
        click.echo(
            f"Detected {refresh.get('agent_native', 0)} agent-native, "
            f"{refresh.get('stalled', 0)} stalled, and "
            f"{refresh.get('productive', 0)} productive loop pattern(s)."
        )


def _require_improvement_snapshot(db_path: Path) -> None:
    """Require an existing prepared snapshot without mutating it."""
    _ensure_command_snapshot(db_path, refresh=False)


@loops.command("show")
@click.argument("loop_id", shell_complete=complete_loop_id)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def loops_show(loop_id: str, as_json: bool, db_path: Path) -> None:
    """Show one loop's classification and bounded source-session evidence."""
    _require_improvement_snapshot(db_path)
    conn, improvement_service = _open_improvement_service(db_path, read_only=True)
    try:
        detail = improvement_service.loops.show(loop_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if as_json:
        _echo_json(detail.model_dump(mode="json"))
        return
    loop = detail.loop
    click.echo(f"{loop.title} ({loop.id})")
    click.echo(
        f"Kind: {loop.kind.value} · State: {loop.status.value} · "
        f"Confidence: {loop.confidence:.0%}"
    )
    click.echo(loop.summary)
    if loop.kind.value == "agent_native":
        click.echo(
            f"{loop.affected_session_count} session(s) · "
            f"{loop.occurrence_count} native continuation signal(s)"
        )
    else:
        click.echo(
            f"{loop.affected_session_count} session(s) · "
            f"{loop.occurrence_count} repeated action(s) · "
            f"{loop.state_change_count} observed state change(s)"
        )
    for occurrence in detail.occurrences[:20]:
        outcome = f" · {occurrence.outcome}" if occurrence.outcome else ""
        click.echo(
            f"  {occurrence.session_id} · {occurrence.repeat_count} repeats · "
            f"{occurrence.error_count} errors{outcome}"
        )


@loops.command("build")
@click.argument("loop_id", shell_complete=complete_loop_id)
@click.option(
    "--agent",
    default=None,
    help="Coding-agent CLI used to author the skill draft.",
    shell_complete=_complete_skill_agent_cli,
)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def loops_build(loop_id: str, agent: str | None, as_json: bool, db_path: Path) -> None:
    """Ask an agent to draft one pending skill from a selected loop."""
    import json as _json
    import subprocess

    _require_improvement_snapshot(db_path)
    conn, improvement_service = _open_improvement_service(db_path, read_only=True)
    try:
        detail = improvement_service.loops.show(loop_id, limit=50)
        evidence_bundle = improvement_service.loops.evidence_bundle(loop_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    agent_bin, agent_flags = _resolve_skills_agent(agent)
    try:
        prompt_template = (
            importlib_resources.files("reflect") / "data" / "loop-skill-prompt.md"
        ).read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError) as exc:
        raise click.ClickException(f"Could not load loop skill prompt: {exc}") from exc
    prompt = prompt_template.replace(
        "{{EVIDENCE_JSON}}",
        _json.dumps(evidence_bundle, sort_keys=True, indent=2),
    )
    try:
        result = subprocess.run(
            [agent_bin, *agent_flags, prompt],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise click.ClickException(f"Could not run agent CLI '{agent_bin}': {exc}") from exc
    if result.returncode != 0:
        raise click.ClickException(
            f"Agent exited with code {result.returncode}: "
            f"{_format_agent_failure(result.stderr, result.stdout)}"
        )
    try:
        extracted = _load_extracted_skills(result.stdout)
    except _json.JSONDecodeError as exc:
        raise click.ClickException(f"Could not parse agent output as JSON: {exc}") from exc
    if len(extracted) != 1:
        raise click.ClickException("Loop build must return exactly one skill draft")
    skill = extracted[0]
    try:
        skill_name = _validate_skill_name(skill.get("name"))
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    staged = {
        **skill,
        "name": skill_name,
        "behavior_type": "loop",
        "source_kind": "agent_authored",
        "source_loop_id": loop_id,
    }
    session_ids = sorted({item.session_id for item in detail.occurrences})
    conn, improvement_service = _open_improvement_service(db_path)
    try:
        candidate_id = improvement_service.stage_extracted_skills(
            [staged],
            session_ids=session_ids,
            source_agent=agent_bin,
        )[0]
        registered = improvement_service.skills.skill_for_candidate(candidate_id)
        improvement_service.loops.mark_promoted(loop_id, registered.id)
    finally:
        conn.close()
    payload = {
        "loop_id": loop_id,
        "skill_id": registered.id,
        "skill_slug": registered.slug,
        "source_agent": agent_bin,
        "status": "pending",
    }
    if as_json:
        _echo_json(payload)
        return
    click.echo(f"Staged {registered.slug} ({registered.id}) from {loop_id}")
    click.echo(f"Review: reflect skills show {registered.id}")
    click.echo("Nothing was installed. Apply only after review with reflect skills apply <ID>.")


@main.command("feedback")
@click.argument("session_id", shell_complete=complete_session_id)
@click.option(
    "--outcome",
    type=click.Choice(["good", "bad", "no-change-correct", "corrected"], case_sensitive=False),
    required=True,
)
@click.option("--reason", default=None, help="Optional concise reason; stored only in the local database.")
@click.option(
    "--refresh/--no-refresh",
    default=False,
    help="Explicitly ingest telemetry before recording; never refreshes by default.",
)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def feedback(
    session_id: str,
    outcome: str,
    reason: str | None,
    refresh: bool,
    db_path: Path,
) -> None:
    """Record an explicit outcome for SESSION_ID."""
    if refresh:
        _ensure_command_snapshot(db_path, refresh=True)
    elif not db_path.exists() or db_path.stat().st_size == 0:
        raise click.ClickException(
            f"Session {session_id} is not available in the local snapshot. "
            "Run this command with --refresh after telemetry ingestion."
        )
    conn, service = _open_improvement_service(db_path)
    try:
        feedback_id = service.repository.record_feedback(
            session_id,
            outcome.lower(),
            reason_redacted=reason,
        )
    except KeyError as exc:
        raise click.ClickException(
            f"{exc}. Re-run with --refresh if this session has not been ingested yet."
        ) from exc
    finally:
        conn.close()
    click.echo(f"Recorded {outcome.lower()} feedback for {session_id} ({feedback_id})")


def _report_server_daemon(
    *,
    db_path: Path,
    otlp_traces: Path | None = None,
    refresh: bool = False,
):
    from reflect.report_server import ReportServerConfig, ReportServerDaemon

    port = int(os.environ.get("REFLECT_PORT", "8765"))
    config = ReportServerConfig(
        port=port,
        db_path=db_path.expanduser().resolve(),
        otlp_traces=otlp_traces.expanduser().resolve() if otlp_traces is not None else None,
        refresh=refresh,
    )
    return ReportServerDaemon(config, state_dir=REFLECT_HOME / "state")


def _start_background_report_server(
    *,
    db_path: Path,
    otlp_traces: Path | None = None,
    refresh: bool,
) -> None:
    import webbrowser

    from rich.console import Console

    daemon = _report_server_daemon(
        db_path=db_path,
        otlp_traces=otlp_traces,
        refresh=refresh,
    )
    console = Console(force_terminal=True)
    if (
        not refresh
        and not daemon.status().running
        and not _has_sql_report_snapshot(db_path)
    ):
        raise click.ClickException(
            f"No report snapshot found at {db_path}. "
            "Run `reflect server --refresh start` to create one."
        )
    existing_status = daemon.status()
    if refresh and existing_status.running and not existing_status.refresh:
        console.print("[yellow]Restarting the snapshot-only report server with refresh enabled.[/]")
        daemon.stop()
    try:
        pid, started = daemon.start()
    except RuntimeError as exc:
        status = daemon.status()
        console.print(f"[yellow]{exc}.[/]")
        console.print(f"  Existing address: [link={status.url}]{status.url}[/link]")
        console.print("  Choose another port with REFLECT_PORT=<port> reflect")
        return
    status = daemon.status()
    if started:
        console.print(f"[green]\u2713[/] Reflect dashboard started in the background (PID {pid})")
    else:
        console.print(f"[green]\u2713[/] Reflect dashboard is already running (PID {pid})")
        if refresh:
            try:
                refresh_result = daemon.request_refresh()
            except (OSError, RuntimeError, ValueError) as exc:
                console.print(f"[yellow]Could not request a dashboard refresh: {exc}[/]")
            else:
                refresh_state = refresh_result.get("preparation") or {}
                state = refresh_state.get("state", "requested") if isinstance(refresh_state, dict) else "requested"
                console.print(f"  Refresh:   {state}")
        webbrowser.open(status.url)
    console.print(f"  Dashboard: [link={status.url}]{status.url}[/link]")
    console.print(f"  Log:       {status.log_file}")
    console.print("  Manage:    reflect server status | reflect server stop")


# ---------------------------------------------------------------------------
# Report command
# ---------------------------------------------------------------------------


def _has_sql_report_snapshot(db_path: Path) -> bool:
    from reflect.preparation import SQLiteSnapshotInspector
    from reflect.store.sqlite import connect_sqlite_read_only

    if not SQLiteSnapshotInspector(db_path).inspect().ready:
        return False
    try:
        conn = connect_sqlite_read_only(db_path)
        try:
            return bool(conn.execute("SELECT 1 FROM session_rollups LIMIT 1").fetchone())
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _render_preparation_summary(console: Console, preparation: dict[str, object]) -> None:
    ingest = preparation["ingest"]
    normalize = preparation["normalize"]
    rollups = preparation["rollups"]
    assert isinstance(ingest, dict)
    assert isinstance(normalize, dict)
    assert isinstance(rollups, dict)
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    for label, value in (
        ("Inserted", ingest["inserted"]),
        ("Skipped", ingest["skipped"]),
        ("Normalized", normalize["processed"]),
        ("Sessions", rollups["session_rollups"]),
    ):
        summary.add_row(label, f"{int(value):,}")
    console.print(Panel(summary, title="[bold orange3]REFLECT[/bold orange3]", border_style="orange3"))


def _run_browser_report(
    *,
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    output: Path | None,
    db_path: Path,
    refresh: bool,
    open_browser: bool = True,
) -> None:
    console = Console()
    update_notice = _build_startup_update_notice()
    if update_notice:
        click.echo(f"reflect notice: {update_notice}")

    include_native_sessions = False
    if refresh:
        if demo:
            demo_traces = Path(__file__).parent / "data" / "demo-traces.json"
            if not demo_traces.exists():
                demo_traces = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
            otlp_traces = demo_traces if demo_traces.exists() else otlp_traces
        elif otlp_traces is None:
            otlp_traces = _default_otlp_traces()
            include_native_sessions = True
        else:
            default_otlp = _default_otlp_traces()
            include_native_sessions = (
                default_otlp is not None
                and otlp_traces.expanduser().resolve() == default_otlp.expanduser().resolve()
            )
    preparation_worker = None
    requires_fresh_snapshot = bool(
        output is not None
        or not _has_sql_report_snapshot(db_path)
    )
    if not refresh:
        if requires_fresh_snapshot:
            raise click.ClickException(
                f"No report snapshot found at {db_path}. "
                "Enable refresh to create one."
            )
        click.echo("Serving the current snapshot without refreshing telemetry.")
    elif requires_fresh_snapshot:
        with console.status("[bold orange3]reflecting...[/bold orange3]", spinner="dots"):
            preparation = _prepare_sql_report_db(
                db_path,
                otlp_traces=otlp_traces,
                include_native_sessions=include_native_sessions,
            )
        _render_preparation_summary(console, preparation)
    else:
        from reflect.preparation import BackgroundPreparationWorker

        def prepare_in_background():
            assert preparation_worker is not None
            return _prepare_sql_report_db(
                db_path,
                otlp_traces=otlp_traces,
                include_native_sessions=include_native_sessions,
                progress=preparation_worker.report_progress,
                defer_otlp_replay=True,
            )

        preparation_worker = BackgroundPreparationWorker(prepare_in_background)
        click.echo("Serving the current snapshot; refreshing telemetry in the background.")

    sessions_dir = sessions_dir or _default_sessions_dir()
    spans_dir = spans_dir or _default_spans_dir()
    if output is not None:
        stats, _, sessions_dir, spans_dir, _, _ = _resolve_and_analyze(
            otlp_traces=otlp_traces,
            sessions_dir=sessions_dir,
            spans_dir=spans_dir,
            demo=demo,
            time_range=time_range,
        )
        render_report(stats, sessions_dir, spans_dir, output)
        print(f"Report saved to: {output}")
    _start_publish_server(
        db_path=db_path,
        preparation_worker=preparation_worker,
        open_browser=open_browser,
    )


# ---------------------------------------------------------------------------
# Skills command
# ---------------------------------------------------------------------------

# Known agent CLIs with their non-interactive (print-mode) flags.
# First entry in the list is the auto-detection priority order.
_SKILL_AGENT_SPECS: list[tuple[str, list[str]]] = [
    ("claude", ["--print"]),
    (
        "agy",
        [
            "--output-format",
            "text",
            "--sandbox",
            "--disable-slash-commands",
            "--print",
        ],
    ),
    ("codex", ["exec"]),
    ("cursor-agent", ["--print", "--trust", "--mode", "ask"]),
    ("copilot", ["--prompt"]),
    ("opencode", ["run"]),
    ("qwen", ["--print"]),
]
_SKILL_AGENT_NAMES = ", ".join(name for name, _ in _SKILL_AGENT_SPECS)
_AGENT_ERROR_LIMIT = 1500


def _format_agent_failure(stderr: str, stdout: str = "") -> str:
    text = (stderr or stdout or "").strip()
    if not text:
        return "(no output)"
    if len(text) <= _AGENT_ERROR_LIMIT:
        return text
    return (
        text[:_AGENT_ERROR_LIMIT]
        + f"\n... truncated {len(text) - _AGENT_ERROR_LIMIT} characters ..."
    )


def _skill_agent_ready(agent: str) -> bool:
    if agent != "cursor-agent":
        return True
    try:
        result = subprocess.run(
            [agent, "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


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
        hint = "type one label" if not multi else "comma-separated labels, empty=all"
        click.echo(hint)
        label_map = {_agent_key(label): index for index, label in enumerate(items)}
        for label in items:
            click.echo(f"  - {label}")
        if multi:
            raw = click.prompt("Select by label (empty for all)", default="", show_default=False)
            if not raw.strip():
                return list(range(n))
            picked = []
            for part in raw.split(","):
                key = _agent_key(part.strip())
                if key in label_map:
                    picked.append(label_map[key])
            return sorted(set(picked)) or list(range(n))
        choice = click.prompt("Select by label", default=items[0], show_default=True)
        return [label_map.get(_agent_key(choice), 0)]

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
        if shutil.which(agent) is None:
            supported = ", ".join(name for name, _flags in _SKILL_AGENT_SPECS)
            raise click.ClickException(
                f"Agent CLI '{agent}' was not found on PATH. "
                f"Install it or choose an installed CLI. Supported names: {supported}."
            )
        for name, flags in _SKILL_AGENT_SPECS:
            if name == agent:
                return agent, flags
        return agent, ["--print"]

    # Probe all supported CLIs
    available = [
        (name, flags)
        for name, flags in _SKILL_AGENT_SPECS
        if shutil.which(name) and _skill_agent_ready(name)
    ]

    if not available:
        raise click.ClickException(
            f"No supported agent CLI found (tried: {_SKILL_AGENT_NAMES}). "
            "Install one or pass --agent with an installed executable."
        )

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
    """Let the user pick which extracted skills to stage for review.

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
            "Select skills to stage (e.g. 1,3) or press Enter for all",
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


def _select_skill_install_agents(
    agents: list[dict],
    console: object,
    *,
    yes: bool,
) -> list[dict]:
    """Choose which detected agents should receive extracted skills."""
    import sys

    if not agents:
        return []
    if yes or len(agents) == 1 or not sys.stdin.isatty():
        return agents

    console.print("\nInstall extracted skills to which agent(s)?\n")
    labels = [
        f"[cyan]{agent['name']:<22}[/cyan] {agent.get('global_path', '')}"
        for agent in agents
    ]
    indices = _interactive_pick(labels, multi=True)
    if not indices:
        console.print("\n[yellow]No agents selected. Aborted.[/yellow]")
        raise SystemExit(0)
    return [agents[i] for i in indices]

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


@main.group(invoke_without_command=True)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used to derive SQL graph evidence for skill extraction.",
)
@click.option("--json", "as_json", is_flag=True, help="Return the Skills v2 registry as JSON.")
@click.option(
    "--status",
    "lifecycle",
    type=click.Choice(["pending", "active", "stale", "retired", "rejected"]),
    help="Filter the registry by lifecycle state.",
)
@click.pass_context
def skills(
    ctx: click.Context,
    db_path: Path,
    as_json: bool,
    lifecycle: str | None,
) -> None:
    """Inspect the durable Skills v2 registry without reconciling sources."""
    if ctx.invoked_subcommand:
        return
    from reflect.improvements.models import SkillLifecycleState

    _require_snapshot_schema(
        db_path,
        refresh_hint=(
            f"Run `reflect skills sync --db-path {db_path}` to create or "
            "reconcile the skill registry."
        ),
    )
    conn, improvement_service = _open_improvement_service(db_path, read_only=True)
    try:
        records = improvement_service.skills.list(
            lifecycle=SkillLifecycleState(lifecycle) if lifecycle else None,
            limit=500,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json(
            {
                "refresh": None,
                "skills": [item.model_dump(mode="json") for item in records],
            }
        )
        return
    _print_skill_registry(records)


@skills.command("sync")
@click.option(
    "--path",
    "scan_paths",
    type=click.Path(path_type=Path),
    multiple=True,
    help="Additional skill root or SKILL.md to reconcile into the registry.",
)
@click.option(
    "--status",
    "lifecycle",
    type=click.Choice(["pending", "active", "stale", "retired", "rejected"]),
    help="Filter the returned registry by lifecycle state.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the sync result as JSON.")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def skills_sync(
    scan_paths: tuple[Path, ...],
    lifecycle: str | None,
    as_json: bool,
    db_path: Path,
) -> None:
    """Reconcile skill files, workflow drafts, usage, and measurements."""
    from reflect.improvements.models import SkillLifecycleState

    conn, improvement_service = _open_improvement_service(db_path)
    try:
        roots = list(scan_paths) or _default_skill_scan_paths()
        refresh_result = improvement_service.skills.refresh(scan_paths=roots)
        records = improvement_service.skills.list(
            lifecycle=SkillLifecycleState(lifecycle) if lifecycle else None,
            limit=500,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json(
            {
                "refresh": refresh_result,
                "skills": [item.model_dump(mode="json") for item in records],
            }
        )
        return
    _print_skill_registry(records, refresh=refresh_result)


def _discover_skills(
    *,
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    agent: str | None,
    yes: bool,
    db_path: Path,
    refresh: bool | None,
) -> None:
    """Run bounded agent-assisted discovery and stage versioned skill drafts."""
    import json as _json
    import subprocess

    from rich.console import Console
    console = Console(force_terminal=True)

    agent_bin, agent_flags = _resolve_skills_agent(agent)

    with console.status("[bold orange3]reflecting...[/bold orange3]", spinner="dots"):
        stats, _, _, _, _, _ = _resolve_and_analyze(
            otlp_traces=otlp_traces,
            sessions_dir=sessions_dir,
            spans_dir=spans_dir,
            demo=demo,
            time_range=time_range,
        )

        try:
            prompt_pkg = importlib_resources.files("reflect") / "data" / "skills-extraction-prompt.md"
            prompt_text = prompt_pkg.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError) as exc:
            click.echo(
                f"Could not load skills extraction prompt: {exc}",
                err=True,
            )
            raise SystemExit(1) from exc
    bundle = _build_skill_evidence_bundle(stats)
    sql_bundle_used = False
    graph_evidence_attached = False
    try:
        from sqlite3 import Error as _SQLiteError

        from reflect.store.sqlite import connect_sqlite_read_only

        include_native_sessions = False
        sql_otlp_traces = otlp_traces
        if demo:
            demo_traces = Path(__file__).parent / "data" / "demo-traces.json"
            if not demo_traces.exists():
                demo_traces = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
            sql_otlp_traces = demo_traces if demo_traces.exists() else otlp_traces
        elif sql_otlp_traces is None:
            sql_otlp_traces = _default_otlp_traces()
            include_native_sessions = True
        else:
            default_otlp = _default_otlp_traces()
            include_native_sessions = (
                default_otlp is not None
                and sql_otlp_traces.expanduser().resolve() == default_otlp.expanduser().resolve()
            )

        snapshot = _improvement_snapshot_status(db_path)
        if demo or refresh is True:
            _ensure_command_snapshot(
                db_path,
                refresh=True,
                otlp_traces=sql_otlp_traces,
                include_native_sessions=include_native_sessions,
            )
        elif not snapshot.ready:
            raise _SQLiteError(
                "prepared snapshot unavailable; run `reflect skills discover --refresh` "
                "to include SQL graph evidence"
            )
        conn = connect_sqlite_read_only(db_path)
        try:
            sql_bundle = _build_skill_evidence_bundle_from_sql(
                conn,
                session_ids=set(stats.sessions_seen),
            )
        finally:
            conn.close()
        if sql_bundle and sql_bundle.get("sessions"):
            bundle = sql_bundle
            sql_bundle_used = True
            graph_evidence_attached = bool((sql_bundle.get("graph_evidence") or {}).get("recurring_patterns"))
    except (_SQLiteError, OSError, ValueError) as exc:
        click.echo(
            f"SQL graph evidence unavailable for skills extraction: {exc}",
            err=True,
        )

    prompt = _build_skills_extraction_prompt_from_bundle(prompt_text, bundle)

    with console.status(
        f"[bold]Extracting skills with {agent_bin}...[/bold]",
        spinner="dots",
    ):
        try:
            result = subprocess.run(
                [agent_bin, *agent_flags, prompt],
                capture_output=True,
                text=True,
            )
        except OSError as exc:
            raise click.ClickException(
                f"Could not run agent CLI '{agent_bin}': {exc}"
            ) from exc
    if result.returncode != 0:
        click.echo(
            f"Agent exited with code {result.returncode}:\n"
            f"{_format_agent_failure(result.stderr, result.stdout)}",
            err=True,
        )
        raise SystemExit(1)

    try:
        skill_defs = _load_extracted_skills(result.stdout)
    except _json.JSONDecodeError as exc:
        click.echo(
            f"Could not parse agent output as JSON: {exc}\n\nOutput:\n{result.stdout[:500]}",
            err=True,
        )
        raise SystemExit(1) from exc

    selected = _select_skills(skill_defs, console, yes=yes)
    valid_selected: list[dict] = []
    for skill in selected:
        try:
            safe_name = _validate_skill_name(skill.get("name"))
        except ValueError as exc:
            click.echo(f"Skipping invalid skill name: {exc}", err=True)
            continue
        valid_selected.append({**skill, "name": safe_name})
    if not valid_selected:
        raise click.ClickException("No valid workflow candidates were extracted")

    conn, improvement_service = _open_improvement_service(db_path)
    try:
        candidate_ids = improvement_service.stage_extracted_skills(
            valid_selected,
            session_ids=sorted(stats.sessions_seen),
            source_agent=agent_bin,
        )
        registered_skills = [
            improvement_service.skills.skill_for_candidate(candidate_id)
            for candidate_id in candidate_ids
        ]
    finally:
        conn.close()

    if sql_bundle_used:
        console.print("[dim]Included SQL stats + Behavioral Memory Graph evidence in extraction prompt.[/dim]")
    elif graph_evidence_attached:
        console.print("[dim]Included SQL Behavioral Memory Graph evidence in extraction prompt.[/dim]")
    console.print(f"\n[bold green]{len(registered_skills)} skill draft(s) staged in Skills v2.[/bold green]")
    for skill in registered_skills:
        console.print(f"  [cyan]{skill.id}[/cyan]  {skill.slug}  reflect skills show {skill.id}")
    console.print("[dim]Nothing was installed. Apply a reviewed draft with reflect skills apply <ID>.[/dim]")


def _default_skill_scan_paths() -> list[Path]:
    roots = [
        Path.cwd() / ".agents" / "skills",
        Path.home() / ".agents" / "skills",
        Path.home() / ".codex" / "skills",
        Path.home() / ".claude" / "skills",
        Path.home() / ".cursor" / "skills",
    ]
    for agent_info in _detect_agents():
        global_path = agent_info.get("global_path")
        if global_path:
            roots.append(Path(str(global_path)))
    unique: dict[str, Path] = {}
    for root in roots:
        resolved = root.expanduser().resolve()
        if resolved.exists():
            unique[str(resolved)] = resolved
    return list(unique.values())


def _print_skill_registry(records, *, refresh: dict[str, int] | None = None) -> None:
    table = Table(title="Skills v2", border_style="orange3")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Skill")
    table.add_column("Origin")
    table.add_column("Version", justify="right")
    table.add_column("Evidence", justify="right")
    table.add_column("Installs", justify="right")
    table.add_column("Uses", justify="right")
    table.add_column("Measures", justify="right")
    table.add_column("State")
    for item in records:
        origin = item.origin.value.replace("_", " ")
        if item.source_agent:
            origin = f"{origin} ({item.source_agent})"
        table.add_row(
            item.id,
            item.slug,
            origin,
            str(item.current_version or "—"),
            str(item.evidence_count),
            str(item.installation_count),
            str(item.usage_count),
            str(item.measurement_count),
            item.lifecycle_state.value,
        )
    Console(force_terminal=True).print(table)
    if refresh:
        click.echo(
            "Synced "
            f"{refresh.get('filesystem_skills', 0)} filesystem skill(s), "
            f"{refresh.get('workflow_skills', 0)} internal workflow implementation(s), and "
            f"{refresh.get('usage', 0)} observed usage link(s)."
        )


@skills.command("discover")
@click.option("--otlp-traces", type=click.Path(path_type=Path), default=None)
@click.option("--sessions-dir", type=click.Path(path_type=Path), default=None)
@click.option("--spans-dir", type=click.Path(path_type=Path), default=None)
@click.option(
    "--period",
    type=click.Choice(["day", "week", "month", "all"]),
    default="week",
    show_default=True,
    help="Evidence period.",
)
@click.option(
    "--refresh/--no-refresh",
    default=None,
    help="Refresh SQLite evidence first; the default reuses an existing snapshot.",
)
@click.option("--demo", is_flag=True)
@click.option("--agent", default=None, shell_complete=_complete_skill_agent_cli)
@click.option("--yes", "yes", is_flag=True)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def skills_discover(
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    period: str,
    refresh: bool | None,
    demo: bool,
    agent: str | None,
    yes: bool,
    db_path: Path,
) -> None:
    """Use a coding agent to discover evidence-backed pending skill drafts."""
    _discover_skills(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        time_range=period,
        demo=demo,
        agent=agent,
        yes=yes,
        db_path=db_path,
        refresh=refresh,
    )


@skills.command("show")
@click.argument("skill_id", shell_complete=complete_skill_id)
@click.option("--json", "as_json", is_flag=True)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def skills_show(skill_id: str, as_json: bool, db_path: Path) -> None:
    """Show a skill's versions, evidence, installations, and usage summary."""
    _require_snapshot_schema(
        db_path,
        refresh_hint=f"Run `reflect skills sync --db-path {db_path}` first.",
    )
    conn, improvement_service = _open_improvement_service(db_path, read_only=True)
    try:
        detail = improvement_service.skills.show(skill_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    if as_json:
        _echo_json(detail.model_dump(mode="json"))
        return
    skill = detail.skill
    click.echo(f"{skill.slug} ({skill.id})")
    click.echo(f"State: {skill.lifecycle_state.value} · Origin: {skill.origin.value}")
    click.echo(
        f"Versions: {skill.version_count} · Evidence: {skill.evidence_count} · "
        f"Installs: {skill.installation_count} · Uses: {skill.usage_count} · "
        f"Measurements: {skill.measurement_count}"
    )
    click.echo(skill.description)
    for version in detail.versions:
        source = version.source_kind
        if version.source_agent:
            source = f"{source} ({version.source_agent})"
        click.echo(f"  v{version.version} {version.status.value} · {source} · {version.content_hash[:12]}")
    for installation in detail.installations:
        click.echo(f"  {installation.status} · {installation.target_kind} · {installation.path}")


@skills.command("apply")
@click.argument("skill_id", shell_complete=complete_skill_id)
@click.option("--project-root", type=click.Path(path_type=Path), default=Path.cwd)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def skills_apply(skill_id: str, project_root: Path, db_path: Path) -> None:
    """Apply a reviewed pending skill version to a Git repository."""
    conn, improvement_service = _open_improvement_service(db_path)
    try:
        candidate_id = improvement_service.skills.workflow_candidate_for(skill_id)
        result = improvement_service.workflows.apply(candidate_id, project_root=project_root)
        improvement_service.skills.sync_workflow_candidates([candidate_id])
        conn.commit()
        skill = improvement_service.skills.skill_for_candidate(candidate_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Applied {skill.slug} to {result['target_path']}")


@skills.command("rollback")
@click.argument("skill_id", shell_complete=complete_skill_id)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def skills_rollback(skill_id: str, db_path: Path) -> None:
    """Roll back the active installation owned by a Reflect skill version."""
    conn, improvement_service = _open_improvement_service(db_path)
    try:
        candidate_id = improvement_service.skills.workflow_candidate_for(skill_id)
        result = improvement_service.workflows.rollback(candidate_id)
        improvement_service.skills.sync_workflow_candidates([candidate_id])
        conn.commit()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        conn.close()
    click.echo(f"Rolled back {skill_id}; restored {result['target_path']}")


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


def _agent_key(name: str) -> str:
    return name.lower().replace(" ", "-")


def _agent_selection_keys(agent: dict) -> set[str]:
    keys = {_agent_key(str(agent["name"]))}
    keys.update(_agent_key(str(alias)) for alias in agent.get("setup_aliases", []))
    return keys


def _setup_agent_alias_map(agents: list[dict]) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for agent in agents:
        canonical = _agent_key(str(agent["name"]))
        for key in _agent_selection_keys(agent):
            alias_map[key] = canonical
    return alias_map


def _resolve_setup_agent_name_keys(
    agent_names: tuple[str, ...],
    detected_agents: list[dict],
) -> tuple[set[str], set[str]]:
    alias_map = _setup_agent_alias_map(detected_agents)
    selected: set[str] = set()
    unknown: set[str] = set()
    for name in agent_names:
        key = _agent_key(name)
        canonical = alias_map.get(key)
        if canonical is None:
            unknown.add(key)
        else:
            selected.add(canonical)
    return selected, unknown


def _filter_agents_by_keys(agents: list[dict], keys: set[str] | None) -> list[dict]:
    if keys is None:
        return agents
    return [agent for agent in agents if _agent_key(str(agent["name"])) in keys]


def _distribute_skills(
    console,
    *,
    selected_agent_names: set[str] | None = None,
    local_agent_names: set[str] | None = None,
) -> None:
    """Distribute the Reflect helpers and OpenTelemetry skill to detected agents."""
    # Bundle reflect core skills for automatic setup distribution.
    bundled_skills_dir = Path(__file__).parent / "data" / "skills"

    available_skills: dict[str, Path] = {}

    reflect_skill = bundled_skills_dir / "reflect"
    if (reflect_skill / "SKILL.md").exists():
        available_skills["reflect"] = reflect_skill

    reflect_skills_helper = bundled_skills_dir / "reflect-skills"
    if (reflect_skills_helper / "SKILL.md").exists():
        available_skills["reflect-skills"] = reflect_skills_helper

    reflect_usage_helper = bundled_skills_dir / "reflect-usage"
    if (reflect_usage_helper / "SKILL.md").exists():
        available_skills["reflect-usage"] = reflect_usage_helper

    otel_skill = _fetch_opentelemetry_skill(console)
    if otel_skill:
        available_skills["opentelemetry-skill"] = otel_skill

    if not available_skills:
        console.print("  [yellow]\u2022[/] No skills available to distribute.")
        return

    def _remove_legacy_skill_aliases(skill_base: Path) -> None:
        legacy = skill_base / "skills"
        if "reflect-skills" in available_skills and legacy.exists():
            shutil.rmtree(legacy)

    # Filter detected agents
    detected_agents = _filter_agents_by_keys(
        [a for a in _detect_agents() if a.get("detected")],
        selected_agent_names,
    )
    local_agent_names = local_agent_names or set()

    written_global_paths: set[Path] = set()
    for agent in detected_agents:
        # 1. Global path (expanded from ~/...)
        try:
            global_skill_path = Path(agent["global_path"]).expanduser()
            resolved_global_skill_path = global_skill_path.resolve()
            if resolved_global_skill_path in written_global_paths:
                console.print(
                    f"  [dim]•[/] {agent['name']}: global path already populated ({global_skill_path})"
                )
            else:
                written_global_paths.add(resolved_global_skill_path)
                global_skill_path.mkdir(parents=True, exist_ok=True)
                for skill_name, skill_src in available_skills.items():
                    dest = global_skill_path / skill_name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(skill_src, dest)
                _remove_legacy_skill_aliases(global_skill_path)
                console.print(f"  [green]\u2713[/] Distributed skills to [bold]{agent['name']}[/] global path")
        except Exception as e:
            console.print(f"  [red]\u2717[/] Failed to distribute to {agent['name']} global: {e}")

        if _agent_key(str(agent["name"])) not in local_agent_names:
            continue
        try:
            local_skill_base = Path.cwd() / str(agent["local_skill_path"])
            local_skill_base.mkdir(parents=True, exist_ok=True)
            for skill_name, skill_src in available_skills.items():
                dest = local_skill_base / skill_name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(skill_src, dest)
            _remove_legacy_skill_aliases(local_skill_base)
            console.print(f"  [green]\u2713[/] Distributed skills to [bold]{agent['name']}[/] local project path")
        except Exception as e:
            console.print(f"  [red]\u2717[/] Failed to distribute to {agent['name']} local project path: {e}")



def _resolve_setup_agent_selection(
    console,
    *,
    agent_names: tuple[str, ...],
    all_agents: bool,
) -> set[str] | None:
    detected = [agent for agent in _detect_agents() if agent.get("detected")]
    if agent_names:
        selected, unknown = _resolve_setup_agent_name_keys(agent_names, detected)
        if unknown:
            raise click.ClickException(
                "Agent(s) not detected: " + ", ".join(sorted(unknown))
            )
        return selected
    if all_agents or not sys.stdin.isatty() or not detected:
        return None

    console.print("\n[bold]Agents to instrument[/]")
    labels = [
        f"{agent['name']} ({agent.get('support_status') or 'planned'})"
        for agent in detected
    ]
    selected_indexes = _interactive_pick(labels, multi=True)
    if len(selected_indexes) == len(detected):
        return None
    return {_agent_key(str(detected[index]["name"])) for index in selected_indexes}


@main.command()
@click.option(
    "--capture-text/--no-capture-text",
    default=None,
    help=(
        "Opt in or out of storing prompt/response text in hook spans. "
        "Omit to preserve the existing hook setting; default hook behavior is off."
    ),
)
@click.option(
    "--text-capture-mode",
    type=click.Choice(["metadata", "masked", "full"], case_sensitive=False),
    default=None,
    help=(
        "Non-interactive setup choice for local/private hook storage: "
        "metadata=no text, masked=text with redaction, full=unmasked text."
    ),
)
@click.option(
    "--mask-captured-text/--no-mask-captured-text",
    default=True,
    show_default=True,
    help="Mask emails, tokens, and home paths when --capture-text is enabled.",
)
@click.option(
    "--text-max-chars",
    type=click.IntRange(min=1),
    default=None,
    help="Maximum characters of captured prompt/response text per event.",
)
@click.option(
    "--agent",
    "agent_names",
    multiple=True,
    help="Agent to instrument by name/key. Repeat to select multiple. Defaults to an interactive choice in a TTY, otherwise all detected agents.",
    shell_complete=_complete_setup_agent,
)
@click.option(
    "--all-agents",
    is_flag=True,
    help="Instrument all detected agents without prompting.",
)
@click.option(
    "--local-agent",
    "local_agent_names",
    multiple=True,
    help="Also install project-scoped hooks and skills for this selected agent. Repeat to select multiple.",
    shell_complete=_complete_setup_agent,
)
@click.option(
    "--shell-completion/--no-shell-completion",
    default=True,
    show_default=True,
    help="Install autocomplete during setup; use --no-shell-completion to opt out.",
)
@click.option(
    "--autostart/--no-autostart",
    default=None,
    help=(
        "Enable persistent OTLP gateway and report server startup on supported systems. "
        "Defaults to enabled on macOS."
    ),
)
def setup(
    capture_text: bool | None,
    text_capture_mode: str | None,
    mask_captured_text: bool,
    text_max_chars: int | None,
    agent_names: tuple[str, ...],
    all_agents: bool,
    local_agent_names: tuple[str, ...],
    shell_completion: bool | None,
    autostart: bool | None,
) -> None:
    """Install opentelemetry-hooks, configure local data export, and suggest agent enablement."""
    from rich.console import Console
    console = Console(force_terminal=True)
    if text_capture_mode:
        mode = text_capture_mode.lower()
        capture_text = mode != "metadata"
        mask_captured_text = mode == "masked"
    elif capture_text is None and sys.stdin.isatty():
        console.print("\n[bold]Prompt/response text capture[/]")
        console.print("[dim]All reflect setup data is stored locally on this machine; no hosted service receives it.[/]")
        capture_modes = [
            "Metadata only - tokens, models, lengths, and hashes; no prompt/response text",
            "Masked text - local prompt/response text with email/token/home-path masking",
            "Full text - local unmasked prompt/response text",
        ]
        choice = _interactive_pick(capture_modes, multi=False)[0]
        if choice == 0:
            capture_text = False
        elif choice == 1:
            capture_text = True
            mask_captured_text = True
        else:
            capture_text = True
            mask_captured_text = False
    selected_agent_keys = _resolve_setup_agent_selection(console, agent_names=agent_names, all_agents=all_agents)
    detected_agents = [agent for agent in _detect_agents() if agent.get("detected")]
    local_agent_keys, unknown_local_agent_keys = _resolve_setup_agent_name_keys(
        local_agent_names,
        detected_agents,
    )
    if unknown_local_agent_keys:
        raise click.ClickException(
            "Agent(s) not detected: " + ", ".join(sorted(unknown_local_agent_keys))
        )
    unknown_local = local_agent_keys - selected_agent_keys if selected_agent_keys is not None else set()
    if unknown_local:
        raise click.ClickException(
            "--local-agent must also be selected with --agent: " + ", ".join(sorted(unknown_local))
        )
    _instrumentation_run_setup(
        console,
        reflect_home=REFLECT_HOME,
        hook_home=HOOK_HOME,
        detect_agents=_detect_agents,
        distribute_skills=_distribute_skills,
        capture_text=capture_text,
        mask_captured_text=mask_captured_text,
        text_max_chars=text_max_chars,
        selected_agent_names=selected_agent_keys,
        local_agent_names=local_agent_keys,
    )
    if shell_completion is not False:
        manager = ShellCompletionManager(main)
        selected_shell = manager.detect_shell()
        if selected_shell is None:
            console.print(
                "[yellow]Shell autocomplete was not installed because $SHELL is unsupported. "
                "Run `reflect completion --help` for supported shells.[/yellow]"
            )
        else:
            try:
                result = manager.install(selected_shell)
            except OSError as exc:
                console.print(
                    "[yellow]Telemetry setup completed, but shell autocomplete could not be "
                    f"installed: {exc}. Run `reflect completion --install` after fixing shell "
                    "configuration permissions.[/yellow]"
                )
            else:
                state = "installed" if result.changed else "already current"
                console.print(f"[green]✓[/] Shell autocomplete {state}: {result.script_path}")
    if autostart is not False:
        _enable_autostart(console, required=autostart is True)


@main.group("doctor", invoke_without_command=True)
@click.pass_context
def doctor(ctx) -> None:
    """Inspect local reflect health, or run focused doctor checks."""
    if ctx.invoked_subcommand is None:
        _run_doctor()


_DELIVERY_AGENT_ALIASES = {
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "github-copilot": "copilot",
    "openai-codex": "codex",
}


def _delivery_agent_key(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return _DELIVERY_AGENT_ALIASES.get(normalized, normalized)


def _load_agent_delivery(db_path: Path) -> dict[str, tuple[str, int]]:
    """Read the latest non-native event received for each normalized agent."""

    if not db_path.is_file():
        return {}
    from reflect.store.sqlite import connect_sqlite_read_only

    try:
        conn = connect_sqlite_read_only(db_path)
        try:
            rows = conn.execute(
                """
                SELECT
                  COALESCE(
                    NULLIF(json_extract(attrs_json, '$."gen_ai.client.name"'), ''),
                    NULLIF(json_extract(attrs_json, '$."agent.name"'), ''),
                    NULLIF(json_extract(attrs_json, '$."service.name"'), '')
                  ) AS agent,
                  MAX(observed_at) AS last_observed_at,
                  COUNT(*) AS event_count
                FROM raw_events
                WHERE source_type NOT LIKE 'native_%'
                GROUP BY agent
                HAVING agent IS NOT NULL AND agent <> ''
                """
            ).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError):
        return {}
    return {
        _delivery_agent_key(agent): (str(last_observed_at or ""), int(event_count or 0))
        for agent, last_observed_at, event_count in rows
    }


def _delivery_timestamp(value: str) -> str:
    if not value:
        return "never observed"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M UTC")


def _run_doctor() -> None:
    """Inspect local reflect, hook, and agent state and suggest the next telemetry step."""
    from rich import box
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table

    console = Console(force_terminal=True)
    hook_runtime = HookRuntime.discover()
    otel_hook = hook_runtime.executable if hook_runtime else None
    hook_report = hook_runtime.doctor_report() if hook_runtime else None
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
    if hook_report:
        exporter = hook_report.get("exporter")
        exporter = exporter if isinstance(exporter, dict) else {}
        delivery_status = str(exporter.get("status") or hook_report.get("status") or "unknown")
        delivery_markup = (
            f"[green]{delivery_status}[/]"
            if delivery_status in {"healthy", "healthy_recent", "ok"}
            else f"[yellow]{delivery_status}[/]"
        )
        summary.add_row("hook delivery", delivery_markup)
    from reflect.gateway import daemon_status as _gateway_status
    try:
        gateway_health = _gateway_status()
    except (OSError, PermissionError, ValueError):
        gateway_health = {"running": False, "conflict": False}
    if gateway_health.get("running"):
        gateway_summary = f"[green]running (PID {gateway_health['pid']})[/]"
    elif gateway_health.get("conflict"):
        destination = gateway_health.get("listener_traces_path") or "unknown destination"
        gateway_summary = (
            f"[yellow]unmanaged listener (PID {gateway_health.get('listener_pid') or '?'})[/] "
            f"[dim]→ {destination}[/]"
        )
    else:
        gateway_summary = "[red]stopped[/]"
    summary.add_row("otlp gateway", gateway_summary)
    try:
        report_server = _report_server_daemon(
            db_path=REFLECT_HOME / "state" / "reflect.db"
        ).status()
    except (OSError, ValueError):
        report_server = None
    if report_server is not None and report_server.running:
        report_server_status = (
            f"[green]running (PID {report_server.pid})[/]  [dim]{report_server.url}[/]"
        )
    elif report_server is not None and report_server.port_in_use:
        report_server_status = f"[yellow]unmanaged listener[/]  [dim]{report_server.url}[/]"
    else:
        report_server_status = "[red]stopped[/]"
    summary.add_row("report server", report_server_status)
    console.print(Panel(summary, title="Overview", border_style="blue"))
    _render_update_advisor_panel(console, update_advisor)

    from reflect.pricing import load_pricing_status
    pricing_status = load_pricing_status(reflect_home=REFLECT_HOME)
    pricing_table = pricing_status.pricing_table
    pricing_live = pricing_table.source in {"live", "cache"}
    pricing_fallback = not pricing_live and bool(pricing_table.prices)
    pricing_ok = pricing_live or pricing_fallback
    pricing_details = Table.grid(padding=(0, 2))
    pricing_details.add_column(style="bold")
    pricing_details.add_column()
    if pricing_fallback:
        source_markup = f"[yellow]{pricing_table.source} ({len(pricing_table.prices)} model(s)) — sync failed, using bundled fallback[/]"
    else:
        source_markup = _status_markup(
            pricing_ok,
            present=f"{pricing_table.source} ({len(pricing_table.prices)} model(s))",
            missing="missing",
        )
    pricing_details.add_row("source", source_markup)
    pricing_details.add_row("pricing unit", pricing_table.pricing_unit)
    pricing_details.add_row("LiteLLM URL", pricing_status.model_prices_url)
    pricing_details.add_row(
        "cache",
        (
            f"{'fresh' if pricing_status.cache_fresh else 'stale/missing'}"
            + (
                f", age {pricing_status.cache_age_seconds // 3600}h"
                if pricing_status.cache_age_seconds is not None
                else ""
            )
        ),
    )
    pricing_details.add_row("cache path", str(pricing_status.cache_path))
    sample_models = ", ".join(sorted(pricing_table.prices.keys())[:5]) or "none"
    pricing_details.add_row("sample models", sample_models)
    if pricing_status.error:
        pricing_details.add_row("last error", f"[yellow]{pricing_status.error}[/]")
    panel_border = "green" if pricing_live else "yellow"
    console.print(Panel(pricing_details, title="Pricing", border_style=panel_border))

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

    if hook_report:
        delivery = _load_agent_delivery(REFLECT_HOME / "state" / "reflect.db")
        registrations = hook_report.get("registrations")
        registrations = registrations if isinstance(registrations, list) else []
        delivery_table = Table(box=box.SIMPLE, expand=True, show_header=True)
        delivery_table.add_column("Agent", style="bold cyan")
        delivery_table.add_column("Registered", no_wrap=True)
        delivery_table.add_column("Delivered to Reflect", no_wrap=True)
        delivery_table.add_column("Events", justify="right", no_wrap=True)
        for registration in registrations:
            if not isinstance(registration, dict):
                continue
            agent = _delivery_agent_key(registration.get("agent"))
            registered_events = int(registration.get("registered_events") or 0)
            last_observed_at, event_count = delivery.get(agent, ("", 0))
            delivered = (
                f"[green]{_delivery_timestamp(last_observed_at)}[/]"
                if last_observed_at
                else "[yellow]never observed[/]"
            )
            delivery_table.add_row(
                agent,
                f"{registered_events} event(s)" if registered_events else "no",
                delivered,
                str(event_count),
            )
        state = hook_report.get("state")
        state = state if isinstance(state, dict) else {}
        exporter = hook_report.get("exporter")
        exporter = exporter if isinstance(exporter, dict) else {}
        writable = state.get("state_directory_writable")
        state_storage = (
            "writable" if writable is True else "not writable" if writable is False else "unknown"
        )
        detail = (
            f"Exporter: {exporter.get('status') or 'unknown'} · "
            f"pending batches: {int(state.get('batches') or 0)} · "
            f"hook state: {state_storage}. "
            "Registration confirms configuration; only a received event confirms delivery."
        )
        console.print(
            Panel(
                Group(delivery_table, detail),
                title="Hook registration vs delivery",
                border_style="yellow",
            )
        )

    _render_native_otel_panel(console, hook_runtime_config)

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
    matrix_agents = [
        agent
        for agent in agents
        if agent["support_status"] == "Implemented" or agent["name"] in _DOCTOR_MATRIX_PLANNED
    ]
    for agent in matrix_agents:
        integrations.add_row(
            agent["name"],
            agent["env"],
            agent["support_status"],
            agent["telemetry_path"],
            agent["confidence"],
        )
    console.print(Panel(integrations, title="Support matrix", border_style="green"))

    mcp_clients = Table(box=box.SIMPLE, expand=True, show_header=True)
    mcp_clients.add_column("Agent", style="bold cyan")
    mcp_clients.add_column("Surface", no_wrap=True)
    mcp_clients.add_column("Configuration")
    for agent in matrix_agents:
        capability = get_mcp_client_capability(agent["name"])
        if capability is None:
            continue
        mcp_clients.add_row(
            capability.agent_name,
            capability.surface.value,
            capability.config_surface,
        )
    console.print(Panel(mcp_clients, title="MCP client matrix", border_style="green"))

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


@doctor.command("cost")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--alias-path", type=click.Path(path_type=Path), default=None, help="Override model alias JSON path.")
def doctor_cost(db_path: Path, alias_path: Path | None) -> None:
    """Append missing model aliases from SQL data and refresh cost estimates."""
    from rich import box
    from rich.console import Console
    from rich.table import Table

    from reflect.store.cost_refresh import CostRefreshState
    from reflect.store.migrate import migrate
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite

    console = Console(force_terminal=True)
    alias_result = None
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        conn = connect_sqlite(db_path)
        try:
            migrate(conn)
            cost_state = CostRefreshState(conn)
            cost_inputs = cost_state.inspect(alias_path=alias_path)
            alias_result = _ensure_sql_costs(
                conn,
                alias_path=alias_path,
                refresh_inputs=cost_inputs,
            )
            rebuild_rollups(conn)
            if cost_inputs.requires_full_refresh:
                cost_state.mark_current(cost_inputs)
            break
        except sqlite3.OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                raise click.ClickException(str(exc)) from exc
            time.sleep(1.5 * attempt)
        finally:
            conn.close()

    if alias_result is None:
        raise click.ClickException(
            "database is locked; stop concurrent writers and retry `reflect doctor cost`"
        )

    table = Table(box=box.SIMPLE_HEAVY, expand=True)
    table.add_column("Check", style="bold cyan")
    table.add_column("Result", overflow="fold")
    table.add_row("SQLite DB", str(db_path))
    table.add_row("Alias file", str(alias_result.alias_path))
    table.add_row("Observed models", str(alias_result.observed_models))
    table.add_row("Resolved models", str(alias_result.resolved_models))
    table.add_row("New aliases", str(len(alias_result.added_aliases)))
    console.print(table)

    if alias_result.added_aliases:
        alias_table = Table(box=box.SIMPLE, expand=True)
        alias_table.add_column("Observed model", style="cyan", overflow="fold")
        alias_table.add_column("Pricing key", style="green", overflow="fold")
        for observed, target in alias_result.added_aliases.items():
            alias_table.add_row(observed, target)
        console.print(alias_table)
    if alias_result.unresolved_models:
        unresolved = ", ".join(alias_result.unresolved_models[:10])
        if len(alias_result.unresolved_models) > 10:
            unresolved += f", +{len(alias_result.unresolved_models) - 10} more"
        console.print(f"[yellow]Unresolved models:[/] {unresolved}")


def _pipx_upgrade_package(pipx: str, package: str, *, force: bool = False) -> None:
    command = [pipx, "upgrade"]
    if force:
        command.append("--force")
    command.append(package)
    subprocess.check_call(command)


@main.command()
@click.option(
    "--apply",
    is_flag=True,
    help="Upgrade Reflect and migrate its bundled otel-hook command via pipx.",
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
        pipx = shutil.which("pipx")
        if not pipx:
            console.print("[red]pipx is not installed or not on PATH.[/]")
            console.print("Install pipx, then run [bold]pipx upgrade o11y-reflect[/].")
            raise SystemExit(1)
        try:
            console.print(
                "Upgrading [bold]o11y-reflect[/] "
                "(including bundled [bold]opentelemetry-hooks[/])..."
            )
            _pipx_upgrade_package(pipx, "o11y-reflect")
            migration = HookPipxMigrator(pipx).migrate()
            if migration.action == "migrated":
                version = f" {migration.standalone_version}" if migration.standalone_version else ""
                console.print(
                    "[green]✓[/] Removed the standalone "
                    f"[bold]opentelemetry-hooks{version}[/] pipx environment."
                )
            elif migration.action == "repaired":
                console.print(
                    "[green]✓[/] Restored the public [bold]otel-hook[/] command from Reflect."
                )
            else:
                console.print(
                    "[green]✓[/] The public [bold]otel-hook[/] command is already owned by Reflect."
                )
            console.print(
                "[green]Package upgrade and hook validation finished.[/] "
                "Re-run [bold]reflect doctor[/] to refresh the cached status."
            )
        except (subprocess.CalledProcessError, HookMigrationError) as exc:
            console.print(f"[red]pipx upgrade or hook migration failed:[/] {exc}")
            raise SystemExit(getattr(exc, "returncode", 1) or 1) from exc

        if advisor["local_issues"]:
            console.print("Local drift remains. Run [bold]reflect setup[/] to refresh global hooks and skill copies.")
    else:
        if release["update_available"]:
            console.print(
                "Use [bold]reflect update --apply[/] to upgrade Reflect and its bundled hooks."
            )
        else:
            console.print("[green]No newer reflect release is available right now.[/]")
            console.print(
                "Use [bold]reflect update --apply[/] to force-check Reflect, its bundled hooks, "
                "and the otel-hook command handoff."
            )
        if advisor["local_issues"]:
            console.print("For local hook or skill drift, run [bold]reflect setup[/] to refresh global wiring.")
    console.print()


def _get_autostart_manager():
    from reflect.autostart import create_autostart_manager

    return create_autostart_manager(REFLECT_HOME)


def _enable_autostart(console, *, required: bool) -> bool:
    manager = _get_autostart_manager()
    if manager is None:
        if required:
            raise click.ClickException(
                "Persistent service startup is not supported on this operating system."
            )
        return False
    try:
        statuses = manager.enable()
    except (OSError, RuntimeError) as exc:
        if required:
            raise click.ClickException(str(exc)) from exc
        console.print(
            "[yellow]Telemetry setup completed, but automatic service startup could not "
            f"be enabled: {exc}[/yellow]"
        )
        console.print("  Run [bold]reflect autostart enable[/] after fixing the service error.")
        return False
    enabled = sum(status.enabled for status in statuses)
    console.print(
        f"[green]✓[/] Automatic startup enabled for {enabled} Reflect services "
        f"via {manager.platform_name}"
    )
    return True


def _require_autostart_manager():
    manager = _get_autostart_manager()
    if manager is None:
        raise click.ClickException(
            "Persistent service startup is not supported on this operating system."
        )
    return manager


def _render_autostart_status(console, statuses) -> None:
    for status in statuses:
        if status.enabled and status.loaded:
            state = "[green]enabled and loaded[/]"
        elif status.enabled:
            state = "[yellow]enabled for next login[/]"
        elif status.loaded:
            state = "[yellow]loaded without an installed definition[/]"
        else:
            state = "[red]disabled[/]"
        console.print(f"  {status.name:<14} {state}")
        console.print(f"    {status.definition_path}")


@main.group("autostart", invoke_without_command=True)
@click.pass_context
def autostart(ctx: click.Context) -> None:
    """Start the OTLP gateway and report server automatically after login."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(autostart_status)


@autostart.command("enable")
def autostart_enable() -> None:
    """Install and load persistent user services."""
    from rich.console import Console

    manager = _require_autostart_manager()
    try:
        statuses = manager.enable()
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    console = Console(force_terminal=True)
    console.print(f"[green]✓[/] Reflect auto-start enabled via {manager.platform_name}")
    console.print("  Services start after user login and remain local to this account.")
    _render_autostart_status(console, statuses)


@autostart.command("disable")
def autostart_disable() -> None:
    """Unload and remove persistent user services."""
    from rich.console import Console

    manager = _require_autostart_manager()
    try:
        statuses = manager.disable()
    except (OSError, RuntimeError) as exc:
        raise click.ClickException(str(exc)) from exc
    console = Console(force_terminal=True)
    console.print("[green]✓[/] Reflect auto-start disabled")
    _render_autostart_status(console, statuses)


@autostart.command("status")
def autostart_status() -> None:
    """Show persistent service registration state."""
    from rich.console import Console

    manager = _require_autostart_manager()
    console = Console(force_terminal=True)
    console.print(f"Reflect auto-start ({manager.platform_name})")
    _render_autostart_status(console, manager.status())


@main.group(invoke_without_command=True)
@click.option("--port", type=int, default=8765, help="Dashboard listen port (default 8765).")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used by the dashboard.",
)
@click.option(
    "--otlp-traces",
    type=click.Path(path_type=Path),
    default=None,
    help="OTLP JSON traces file to ingest during refresh.",
)
@click.option(
    "--refresh/--no-refresh",
    default=False,
    help="Refresh telemetry before serving. Disabled by default for bounded snapshots.",
)
@click.pass_context
def server(
    ctx: click.Context,
    port: int,
    db_path: Path,
    otlp_traces: Path | None,
    refresh: bool,
) -> None:
    """Manage the background browser report server."""
    if otlp_traces is not None and not refresh:
        raise click.UsageError("--otlp-traces requires --refresh")
    ctx.ensure_object(dict)
    ctx.obj.update({
        "port": port,
        "db_path": db_path,
        "otlp_traces": otlp_traces,
        "refresh": refresh,
    })
    if ctx.invoked_subcommand is None:
        ctx.invoke(server_start)


@server.command("start")
@click.pass_context
def server_start(ctx: click.Context) -> None:
    """Start or open the background browser report server."""
    previous_port = os.environ.get("REFLECT_PORT")
    os.environ["REFLECT_PORT"] = str(ctx.obj["port"])
    try:
        _start_background_report_server(
            db_path=ctx.obj["db_path"],
            otlp_traces=ctx.obj["otlp_traces"],
            refresh=ctx.obj["refresh"],
        )
    finally:
        if previous_port is None:
            os.environ.pop("REFLECT_PORT", None)
        else:
            os.environ["REFLECT_PORT"] = previous_port


@server.command("stop")
@click.pass_context
def server_stop(ctx: click.Context) -> None:
    """Stop the background browser report server."""
    from rich.console import Console

    previous_port = os.environ.get("REFLECT_PORT")
    os.environ["REFLECT_PORT"] = str(ctx.obj["port"])
    try:
        daemon = _report_server_daemon(db_path=ctx.obj["db_path"])
        stopped = daemon.stop()
    finally:
        if previous_port is None:
            os.environ.pop("REFLECT_PORT", None)
        else:
            os.environ["REFLECT_PORT"] = previous_port
    console = Console(force_terminal=True)
    console.print("[green]\u2713[/] Reflect dashboard stopped" if stopped else "[yellow]Reflect dashboard is not running[/]")


@server.command("status")
@click.pass_context
def server_status(ctx: click.Context) -> None:
    """Show browser report server status."""
    from rich.console import Console

    previous_port = os.environ.get("REFLECT_PORT")
    os.environ["REFLECT_PORT"] = str(ctx.obj["port"])
    try:
        status = _report_server_daemon(db_path=ctx.obj["db_path"]).status()
    finally:
        if previous_port is None:
            os.environ.pop("REFLECT_PORT", None)
        else:
            os.environ["REFLECT_PORT"] = previous_port
    console = Console(force_terminal=True)
    if status.running:
        console.print(f"[green]running[/] (PID {status.pid})")
    else:
        console.print("[red]stopped[/]")
    console.print(f"  dashboard: {status.url}")
    console.print(f"  database:  {status.db_path}")
    console.print(f"  refresh:   {'enabled' if status.refresh else 'disabled'}")
    console.print(f"  log:       {status.log_file}")


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
    elif status.get("conflict"):
        console.print(
            f"[yellow]unmanaged listener[/] (PID {status.get('listener_pid') or '?'})"
            f" routes traces to {status.get('listener_traces_path') or 'an unknown destination'}"
        )
    else:
        console.print("[red]stopped[/]")
    console.print(f"  traces: {status['traces_path']} ({_summarize_file(Path(status['traces_path']))})")
    console.print(f"  logs:   {status['logs_path']} ({_summarize_file(Path(status['logs_path']))})")
    console.print(f"  log:    {status['log_file']}")


@main.group()
def memory() -> None:
    """Evidence-backed local and provider memory commands."""


def _open_memory_service(
    db_path: Path,
    *,
    read_only: bool = False,
) -> tuple[sqlite3.Connection, MemoryService]:
    from reflect.memory import MemoryService
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite, connect_sqlite_read_only

    if read_only:
        _require_snapshot_schema(
            db_path,
            refresh_hint=f"Run `reflect memory sync --db-path {db_path}` first.",
        )
        conn = connect_sqlite_read_only(db_path)
    else:
        conn = connect_sqlite(db_path)
        migrate(conn)
    return conn, MemoryService(conn, maintain_search_index=not read_only)


def _memory_filters(
    *,
    type: str | None = None,
    scope: str | None = None,
    source: str | None = None,
    provider: str | None = None,
    stale: bool = False,
    validated: bool = False,
    unvalidated: bool = False,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "type": type,
            "scope": scope,
            "source": source,
            "provider": provider,
            "stale": stale,
            "validated": validated,
            "unvalidated": unvalidated,
        }.items()
        if value
    }


def _echo_json(payload: object) -> None:
    click.echo(_json_stdlib.dumps(payload, indent=2, sort_keys=True))


@memory.command("providers")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--json", "as_json", is_flag=True, help="Print provider health as JSON.")
def memory_providers(db_path: Path, as_json: bool) -> None:
    """List memory providers and health."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        health = service.provider_health()
    finally:
        conn.close()
    if as_json:
        _echo_json(health)
        return
    table = Table(title="Memory Providers")
    table.add_column("Provider")
    table.add_column("Available")
    table.add_column("Status")
    table.add_column("Detail")
    for item in health:
        table.add_row(
            str(item["name"]),
            "yes" if item["available"] else "no",
            str(item["status"]),
            str(item.get("detail") or ""),
        )
    Console().print(table)


@memory.command("sync")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--json", "as_json", is_flag=True, help="Print sync result as JSON.")
def memory_sync(path: Path | None, db_path: Path, as_json: bool) -> None:
    """Sync local folder instruction memories. PATH defaults to the current directory."""
    target = path or Path.cwd()
    conn, service = _open_memory_service(db_path)
    try:
        result = service.sync_path(target, home_root=Path.home())
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    click.echo(
        "Synced memories "
        f"(path={target}, discovered={result['discovered']}, inserted={result['inserted']}, updated={result['updated']})"
    )


@memory.command("list")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--all", "all_memories", is_flag=True, help="List all memories instead of scoping to PATH.")
@click.option("--type", "memory_type", default=None, help="Filter by memory type.", shell_complete=complete_memory_type)
@click.option("--scope", default=None, help="Filter by memory scope.", shell_complete=complete_memory_scope)
@click.option("--source", default=None, help="Filter by memory source.", shell_complete=complete_memory_source)
@click.option("--provider", default=None, help="Filter by provider.", shell_complete=complete_memory_provider)
@click.option("--stale", is_flag=True, help="Only show stale memories.")
@click.option("--validated", is_flag=True, help="Only show validated memories.")
@click.option("--unvalidated", is_flag=True, help="Only show unvalidated memories.")
@click.option("--limit", type=int, default=100, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print memories as JSON.")
def memory_list(
    path: Path | None,
    db_path: Path,
    all_memories: bool,
    memory_type: str | None,
    scope: str | None,
    source: str | None,
    provider: str | None,
    stale: bool,
    validated: bool,
    unvalidated: bool,
    limit: int,
    as_json: bool,
) -> None:
    """List memories for PATH. PATH defaults to the current directory."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        rows = service.list_memories(
            path=path or Path.cwd(),
            all_memories=all_memories,
            filters=_memory_filters(
                type=memory_type,
                scope=scope,
                source=source,
                provider=provider,
                stale=stale,
                validated=validated,
                unvalidated=unvalidated,
            ),
            limit=limit,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    table = Table(title="Reflect Memories")
    for column in ("ID", "Type", "Scope", "Source", "Validation", "Path"):
        table.add_column(column)
    for row in rows:
        metadata = row.get("source_metadata") or {}
        raw_attrs = row.get("raw_attrs") or {}
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("type") or ""),
            str(row.get("scope") or ""),
            str(row.get("source") or ""),
            str(row.get("validation_status") or ""),
            str(metadata.get("path") or raw_attrs.get("path") or ""),
        )
    Console().print(table)


@memory.command("search")
@click.argument("query")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--type", "memory_type", default=None, help="Filter by memory type.", shell_complete=complete_memory_type)
@click.option("--scope", default=None, help="Filter by memory scope.", shell_complete=complete_memory_scope)
@click.option("--provider", default="local_sqlite", show_default=True, help="Provider to search.", shell_complete=complete_memory_provider)
@click.option("--limit", type=int, default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print search results as JSON.")
def memory_search(
    query: str,
    path: Path | None,
    db_path: Path,
    memory_type: str | None,
    scope: str | None,
    provider: str,
    limit: int,
    as_json: bool,
) -> None:
    """Search memories, optionally scoped to PATH."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        rows = service.search(
            query,
            path=path or Path.cwd(),
            filters=_memory_filters(type=memory_type, scope=scope),
            provider=provider,
            limit=limit,
        )
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    table = Table(title=f"Memory Search: {query}")
    for column in ("ID", "Type", "Scope", "Provider", "Preview"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("id") or row.get("memory_id") or ""),
            str(row.get("type") or ""),
            str(row.get("scope") or ""),
            str(row.get("provider") or provider),
            str(row.get("content_preview_redacted") or row.get("content") or "")[:100],
        )
    Console().print(table)


@memory.command("inspect")
@click.argument("memory_id", shell_complete=complete_memory_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--json", "as_json", is_flag=True, help="Print memory as JSON.")
def memory_inspect(memory_id: str, db_path: Path, as_json: bool) -> None:
    """Inspect one memory by ID."""
    conn, service = _open_memory_service(db_path, read_only=True)
    try:
        row = service.inspect(memory_id)
    finally:
        conn.close()
    if row is None:
        raise click.ClickException(f"Memory not found: {memory_id}")
    if as_json:
        _echo_json(row)
        return
    Console().print(Panel(_json_stdlib.dumps(row, indent=2, sort_keys=True), title=memory_id))


@memory.command("forget")
@click.argument("memory_id", shell_complete=complete_memory_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def memory_forget(memory_id: str, db_path: Path) -> None:
    """Delete one local memory by ID."""
    conn, service = _open_memory_service(db_path)
    try:
        removed = service.forget(memory_id)
    finally:
        conn.close()
    if not removed:
        raise click.ClickException(f"Memory not found: {memory_id}")
    click.echo(f"Forgot memory {memory_id}")


@memory.command("validate")
@click.argument("memory_id", required=False, shell_complete=complete_memory_id)
@click.option("--candidate", "candidate_id", default=None, help="Promote and validate a graph-derived candidate.", shell_complete=complete_memory_candidate_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--json", "as_json", is_flag=True, help="Print validation result as JSON.")
def memory_validate(memory_id: str | None, candidate_id: str | None, db_path: Path, as_json: bool) -> None:
    """Validate a memory or promote a candidate."""
    if not memory_id and not candidate_id:
        raise click.ClickException("Pass MEMORY_ID or --candidate CANDIDATE_ID")
    conn, service = _open_memory_service(db_path)
    try:
        if candidate_id:
            promoted = service.promote_candidate(candidate_id)
            result = service.validate(str(promoted["id"]))
        else:
            result = service.validate(str(memory_id))
    finally:
        conn.close()
    if as_json:
        _echo_json(result)
        return
    click.echo(
        f"Memory {result['memory_id']}: {result['status']}"
        + (f" ({result['stale_reason']})" if result.get("stale_reason") else "")
    )


@memory.command("candidates")
@click.argument("path", type=click.Path(path_type=Path), required=False)
@click.option("--session", "session_id", default="", help="Limit candidates to one session ID.", shell_complete=complete_session_id)
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print candidates as JSON.")
def memory_candidates(
    path: Path | None,
    session_id: str,
    db_path: Path,
    limit: int,
    as_json: bool,
) -> None:
    """List graph-derived memory candidates for PATH."""
    conn, service = _open_memory_service(db_path)
    try:
        rows = service.candidates(path=path or Path.cwd(), session_id=session_id, limit=limit)
    finally:
        conn.close()
    if as_json:
        _echo_json(rows)
        return
    table = Table(title="Memory Candidates")
    for column in ("ID", "Type", "Confidence", "Content"):
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row.get("id") or ""),
            str(row.get("type") or ""),
            f"{float(row.get('confidence') or 0):.2f}",
            str(row.get("content") or "")[:120],
        )
    Console().print(table)


@main.group()
def db() -> None:
    """SQLite store management commands."""


def _ingest_into_db(
    *,
    db_path: Path,
    otlp_traces: Path | None = None,
    spans_file: Path | None = None,
) -> dict[str, int]:
    from reflect.store.cost_refresh import CostRefreshState
    from reflect.store.ingest import ingest_local_spans_file, ingest_otlp_traces_file
    from reflect.store.migrate import migrate
    from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite

    if (otlp_traces is None) == (spans_file is None):
        raise click.ClickException("Pass exactly one of --otlp or --spans-file")

    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        if otlp_traces is not None:
            result = ingest_otlp_traces_file(conn, file_path=otlp_traces)
        else:
            result = ingest_local_spans_file(conn, file_path=spans_file)
        changed_session_ids: set[str] = set()
        normalize_pending_raw_events(conn, changed_session_ids=changed_session_ids)
        backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
        )
        cost_state = CostRefreshState(conn)
        cost_inputs = cost_state.inspect(session_ids=changed_session_ids)
        _ensure_sql_costs(conn, refresh_inputs=cost_inputs)
        rebuild_rollups(conn)
        if cost_inputs.requires_full_refresh:
            cost_state.mark_current(cost_inputs)
    finally:
        conn.close()
    return result


def _ensure_sql_costs(
    conn,
    *,
    alias_path: Path | None = None,
    session_ids: set[str] | None = None,
    refresh_inputs=None,
):
    from reflect.config import load_model_aliases
    from reflect.cost_aliases import ensure_cost_aliases
    from reflect.pricing import load_pricing_table

    if refresh_inputs is None:
        pricing_table = load_pricing_table()
        alias_result = ensure_cost_aliases(
            conn,
            alias_path=alias_path,
            pricing_table=pricing_table,
            session_ids=session_ids,
        )
        aliases = load_model_aliases(alias_result.alias_path)
    else:
        pricing_table = refresh_inputs.pricing_table
        alias_result = refresh_inputs.alias_result
        aliases = refresh_inputs.aliases
        if refresh_inputs.requires_full_refresh:
            session_ids = None
    _reprice_sql_store(
        conn,
        alias_path=alias_result.alias_path,
        session_ids=session_ids,
        pricing_table=pricing_table,
        aliases=aliases,
    )
    return alias_result


def _cursor_native_parent_session_id(source_ref: str) -> str:
    match = re.search(r"/agent-transcripts/([^/]+)/", source_ref or "")
    return match.group(1) if match else ""


def _prepare_sql_report_db(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool = False,
    spans_dir: Path | None = None,
    progress: PreparationProgressReporter | None = None,
    defer_otlp_replay: bool = False,
) -> dict[str, object]:
    from reflect.preparation import PreparationStage, report_preparation_progress
    from reflect.store.cost_refresh import CostRefreshState
    from reflect.store.cursor_adapter import (
        apply_cursor_transcript_usage_estimates,
        repair_misattributed_cursor_transcript_usage,
    )
    from reflect.store.graph_normalize import rebuild_graph, refresh_graph
    from reflect.store.ingest import (
        AppendOnlyReplayPolicy,
        ingest_codex_context_file,
        ingest_local_spans_file,
        ingest_native_session_file,
        ingest_otlp_logs_file,
        ingest_otlp_traces_file,
    )
    from reflect.store.migrate import migrate
    from reflect.store.normalize import (
        backfill_mcp_calls,
        backfill_tool_call_hashes,
        normalize_pending_raw_events,
    )
    from reflect.store.refresh_plan import RefreshMode, plan_derived_refresh
    from reflect.store.rollups import rebuild_rollups, refresh_rollups, rollup_rebuild_pending
    from reflect.store.sqlite import connect_sqlite
    from reflect.store.workspaces import backfill_session_context

    report_preparation_progress(
        progress,
        PreparationStage.OPENING_STORE,
        "Opening the local telemetry store...",
    )
    conn = connect_sqlite(db_path)
    try:
        report_preparation_progress(
            progress,
            PreparationStage.MIGRATING_SCHEMA,
            "Checking database migrations...",
        )
        applied = migrate(conn)
        ingest_result = {"inserted": 0, "skipped": 0}
        ingest_sources: dict[str, dict[str, object]] = {}
        cursor_native_files: list[Path] = []
        replay_policy = (
            AppendOnlyReplayPolicy.DEFER
            if defer_otlp_replay
            else AppendOnlyReplayPolicy.REPLAY
        )
        if otlp_traces is not None and otlp_traces.exists():
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new OTLP traces...",
            )
            traces_result = ingest_otlp_traces_file(
                conn,
                file_path=otlp_traces,
                skip_unchanged=True,
                replay_policy=replay_policy,
            )
            ingest_sources["otlp_traces"] = traces_result
            ingest_sources["otlp_traces"]["source_type"] = "otlp_traces_json"
            ingest_result["inserted"] += traces_result["inserted"]
            ingest_result["skipped"] += traces_result["skipped"]
            otlp_logs = _infer_otlp_logs_file(otlp_traces)
            if otlp_logs is not None and otlp_logs.exists():
                report_preparation_progress(
                    progress,
                    PreparationStage.INGESTING_LOGS,
                    "Reading new OTLP logs...",
                )
                logs_result = ingest_otlp_logs_file(
                    conn,
                    file_path=otlp_logs,
                    skip_unchanged=True,
                    replay_policy=replay_policy,
                )
                ingest_sources["otlp_logs"] = logs_result
                ingest_sources["otlp_logs"]["source_type"] = "otlp_logs_json"
                ingest_result["inserted"] += logs_result["inserted"]
                ingest_result["skipped"] += logs_result["skipped"]
        if spans_dir is not None and spans_dir.is_dir():
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new hook spans...",
            )
            spans_result: dict[str, object] = {
                "inserted": 0,
                "skipped": 0,
                "unchanged": 0,
                "files": 0,
                "source_type": "local_spans_jsonl",
            }
            for span_file in sorted(spans_dir.glob("*.jsonl")):
                file_result = ingest_local_spans_file(
                    conn,
                    file_path=span_file,
                    skip_unchanged=True,
                )
                spans_result["files"] = int(spans_result["files"]) + 1
                for key in ("inserted", "skipped", "unchanged"):
                    spans_result[key] = int(spans_result[key]) + int(
                        file_result.get(key, 0)
                    )
            ingest_sources["local_spans"] = spans_result
            ingest_result["inserted"] += int(spans_result["inserted"])
            ingest_result["skipped"] += int(spans_result["skipped"])
        if include_native_sessions:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_SESSIONS,
                "Reading local agent sessions...",
            )
            native_result = {"inserted": 0, "skipped": 0, "unchanged": 0}
            context_result = {"inserted": 0, "skipped": 0, "unchanged": 0}
            for agent, session_file in _discover_rich_session_files():
                source_ref = f"native_session:{agent}:{session_file}"
                if agent == "codex":
                    context_file_result = ingest_codex_context_file(
                        conn,
                        file_path=session_file,
                    )
                    for key in context_result:
                        context_result[key] += int(context_file_result.get(key, 0))
                result = ingest_native_session_file(
                    conn,
                    file_path=session_file,
                    agent=agent,
                    source_id=source_ref,
                    skip_existing_sessions=agent != "opencode",
                    skip_unchanged=True,
                )
                native_result["inserted"] += result["inserted"]
                native_result["skipped"] += result["skipped"]
                native_result["unchanged"] += result.get("unchanged", 0)
                if agent == "cursor" and not result.get("unchanged"):
                    cursor_native_files.append(session_file)
            if any(native_result.values()):
                ingest_sources["native_sessions"] = native_result
                ingest_sources["native_sessions"]["source_type"] = "native_session"
                ingest_result["inserted"] += native_result["inserted"]
                ingest_result["skipped"] += native_result["skipped"]
            if any(context_result.values()):
                ingest_sources["context_artifacts"] = context_result
                ingest_sources["context_artifacts"]["source_type"] = "native_context"
                ingest_result["inserted"] += context_result["inserted"]
                ingest_result["skipped"] += context_result["skipped"]
        needs_normalize = bool(
            ingest_result["inserted"]
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE origin_kind IS NULL LIMIT 1"
            ).fetchone()
        )
        changed_session_ids: set[str] = set()
        report_preparation_progress(
            progress,
            PreparationStage.NORMALIZING,
            "Normalizing new telemetry...",
        )
        normalize_result = (
            normalize_pending_raw_events(
                conn,
                changed_session_ids=changed_session_ids,
            )
            if needs_normalize
            else {"processed": 0, "failed": 0, "skipped": 0}
        )
        report_preparation_progress(
            progress,
            PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating canonical session state...",
        )
        fingerprint_result = backfill_tool_call_hashes(conn)
        mcp_backfill_result = backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
            changed_session_ids=changed_session_ids,
        )
        cursor_provenance_result = repair_misattributed_cursor_transcript_usage(conn)
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_provenance_result["session_ids"]
            if session_id
        )
        context_result = backfill_session_context(
            conn,
            timestamp=datetime.now(UTC).isoformat(),
            changed_session_ids=changed_session_ids,
        )
        cursor_adapter_result = apply_cursor_transcript_usage_estimates(
            conn,
            cursor_native_files,
        )
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_adapter_result.get("session_ids", [])
            if session_id
        )
        reconciled_legacy_data = rollup_rebuild_pending(conn)
        all_session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
        rollup_session_ids = {
            str(row[0]) for row in conn.execute("SELECT session_id FROM session_rollups")
        }
        graph_session_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session_id FROM graph_nodes "
                "WHERE kind = 'Session' AND session_id IS NOT NULL"
            )
        }
        cost_state = CostRefreshState(conn)
        cost_inputs = (
            cost_state.inspect(session_ids=changed_session_ids)
            if all_session_ids
            else None
        )
        refresh_plan = plan_derived_refresh(
            changed_session_ids=changed_session_ids,
            all_session_ids=all_session_ids,
            graph_session_ids=graph_session_ids,
            rollup_session_ids=rollup_session_ids,
            graph_exists=bool(graph_session_ids),
            force_full_cost_reason=(
                cost_inputs.reason
                if cost_inputs is not None and cost_inputs.requires_full_refresh
                else ""
            ),
            force_full_rollup_reason=(
                "Codex Desktop telemetry migration requires reconciliation"
                if reconciled_legacy_data
                else ""
            ),
        )

        if refresh_plan.cost_mode is RefreshMode.FULL:
            _ensure_sql_costs(conn, refresh_inputs=cost_inputs)
        elif refresh_plan.cost_mode is RefreshMode.INCREMENTAL:
            _ensure_sql_costs(
                conn,
                session_ids=set(refresh_plan.cost_session_ids),
                refresh_inputs=cost_inputs,
            )

        if refresh_plan.graph_mode is RefreshMode.FULL:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_GRAPH,
                f"Rebuilding the evidence graph ({refresh_plan.graph_reason})...",
            )
            graph_result = rebuild_graph(conn)
        elif refresh_plan.graph_mode is RefreshMode.INCREMENTAL:
            graph_targets = set(refresh_plan.graph_session_ids)
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_GRAPH,
                f"Refreshing the evidence graph for {len(graph_targets):,} session(s)...",
            )
            graph_result = refresh_graph(conn, graph_targets)
        else:
            graph_result = {
                "nodes": 0,
                "edges": 0,
                "skipped": 1,
                "reason": refresh_plan.graph_reason,
            }

        if refresh_plan.rollup_mode is RefreshMode.FULL:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Rebuilding session rollups ({refresh_plan.rollup_reason})...",
            )
            rollup_result = rebuild_rollups(conn)
        elif refresh_plan.rollup_mode is RefreshMode.INCREMENTAL:
            rollup_targets = set(refresh_plan.rollup_session_ids)
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Refreshing rollups for {len(rollup_targets):,} session(s)...",
            )
            rollup_result = refresh_rollups(conn, rollup_targets)
        else:
            rollup_result = {
                "session_rollups": len(rollup_session_ids),
                "daily_rollups": int(conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0]),
                "tool_rollups": int(conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0]),
                "skipped": 1,
                "reason": refresh_plan.rollup_reason,
            }
        if cost_inputs is not None and cost_inputs.requires_full_refresh:
            cost_state.mark_current(cost_inputs)
        from reflect.improvements.service import ImprovementService

        report_preparation_progress(
            progress,
            PreparationStage.REFRESHING_IMPROVEMENTS,
            "Refreshing evidence-backed improvements...",
        )
        improvement_result = ImprovementService(conn).refresh()
        refresh_completed_at = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO store_metadata(key, value, updated_at)
            VALUES ('last_successful_refresh', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (refresh_completed_at, refresh_completed_at),
        )
        conn.commit()
    finally:
        conn.close()
    deferred_replays = [
        name
        for name, source_result in ingest_sources.items()
        if source_result.get("mode") == "replay_deferred"
    ]
    result = {
        "sessions": len(all_session_ids),
        "applied_migrations": applied,
        "ingest": ingest_result,
        "ingest_sources": ingest_sources,
        "normalize": normalize_result,
        "mcp_calls": mcp_backfill_result,
        "tool_call_fingerprints": fingerprint_result,
        "session_context": context_result,
        "cursor_provenance": cursor_provenance_result,
        "cursor_adapter": cursor_adapter_result,
        "refresh_plan": refresh_plan.as_dict(),
        "graph": graph_result,
        "rollups": rollup_result,
        "improvements": improvement_result,
        "deferred_replays": deferred_replays,
    }
    report_preparation_progress(
        progress,
        PreparationStage.COMPLETE,
        "Preparation complete.",
    )
    return result


def _reprice_sql_store(
    conn,
    *,
    alias_path: Path | None = None,
    session_ids: set[str] | None = None,
    pricing_table=None,
    aliases: dict[str, str] | None = None,
) -> None:
    from reflect.config import load_model_aliases
    from reflect.pricing import calculate_cost, load_pricing_table

    pricing_table = pricing_table or load_pricing_table()
    if aliases is None:
        aliases = load_model_aliases(alias_path)
    import sqlite3

    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        scoped_ids = sorted(session_ids) if session_ids is not None else None
        if scoped_ids is not None and not scoped_ids:
            return
        placeholders = ", ".join("?" for _ in scoped_ids or [])
        llm_scope = f"WHERE session_id IN ({placeholders})" if scoped_ids is not None else ""
        selected_session_scope = f"WHERE id IN ({placeholders})" if scoped_ids is not None else ""
        selected_session_rows = conn.execute(
            f"SELECT id, source_ref FROM sessions {selected_session_scope}",
            scoped_ids or [],
        ).fetchall()
        model_scope_ids = set(scoped_ids or [])
        for selected_session in selected_session_rows:
            parent_id = _cursor_native_parent_session_id(str(selected_session["source_ref"] or ""))
            if parent_id:
                model_scope_ids.add(parent_id)
        model_scope_values = sorted(model_scope_ids)
        model_placeholders = ", ".join("?" for _ in model_scope_values)
        model_scope = (
            f"AND session_id IN ({model_placeholders})"
            if scoped_ids is not None
            else ""
        )
        rows = conn.execute(
            f"""
            SELECT
              id,
              session_id,
              COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
              input_tokens,
              output_tokens,
              cache_creation_input_tokens,
              cache_read_input_tokens,
              reasoning_output_tokens
            FROM llm_calls
            {llm_scope}
            """,
            scoped_ids or [],
        ).fetchall()
        model_rows = conn.execute(
            f"""
            SELECT
              session_id,
              COALESCE(
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
              ) AS model,
              COUNT(*) AS count
            FROM steps
            WHERE COALESCE(
              NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
              NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
            ) IS NOT NULL
              {model_scope}
            GROUP BY session_id, model
            ORDER BY session_id ASC, count DESC
            """,
            model_scope_values if scoped_ids is not None else [],
        ).fetchall()
        session_models: dict[str, str] = {}
        for model_row in model_rows:
            session_models.setdefault(model_row["session_id"], model_row["model"])
        seen_usage: set[tuple] = set()
        session_costs: dict[str, float] = {}
        session_tokens: dict[str, dict[str, int]] = {}
        session_model_hints: dict[str, str] = {}
        for row in rows:
            model = row["model"] or session_models.get(row["session_id"], "")
            if model:
                session_model_hints.setdefault(row["session_id"], model)
            usage_key = (
                row["session_id"],
                model,
                int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0),
                int(row["cache_creation_input_tokens"] or 0),
                int(row["cache_read_input_tokens"] or 0),
                int(row["reasoning_output_tokens"] or 0),
            )
            breakdown = calculate_cost(
                {
                    "input": row["input_tokens"],
                    "output": row["output_tokens"],
                    "cache_creation": row["cache_creation_input_tokens"],
                    "cache_read": row["cache_read_input_tokens"],
                },
                model,
                pricing_table,
                aliases=aliases,
            )
            counted = usage_key not in seen_usage
            if counted:
                seen_usage.add(usage_key)
                tokens = session_tokens.setdefault(
                    row["session_id"],
                    {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "reasoning": 0},
                )
                tokens["input"] += int(row["input_tokens"] or 0)
                tokens["output"] += int(row["output_tokens"] or 0)
                tokens["cache_creation"] += int(row["cache_creation_input_tokens"] or 0)
                tokens["cache_read"] += int(row["cache_read_input_tokens"] or 0)
                tokens["reasoning"] += int(row["reasoning_output_tokens"] or 0)
                session_costs[row["session_id"]] = (
                    session_costs.get(row["session_id"], 0.0) + breakdown.total_cost_usd
                )
            conn.execute(
                """
                UPDATE llm_calls
                SET
                  estimated_cost_usd = ?,
                  request_model = CASE
                    WHEN COALESCE(request_model, '') = '' THEN NULLIF(?, '')
                    ELSE request_model
                  END,
                  response_model = CASE
                    WHEN COALESCE(response_model, '') = '' THEN NULLIF(?, '')
                    ELSE response_model
                  END
                WHERE id = ?
                """,
                (
                    breakdown.total_cost_usd if counted else 0.0,
                    model,
                    model,
                    row["id"],
                ),
            )
        timestamp = datetime.now(tz=UTC).isoformat()
        session_scope = f"AND id IN ({placeholders})" if scoped_ids is not None else ""
        session_level_rows = conn.execute(
            f"""
            SELECT
              id,
              source_ref,
              input_tokens,
              output_tokens,
              cache_creation_tokens,
              cache_read_tokens,
              reasoning_tokens
            FROM sessions
            WHERE COALESCE(input_tokens, 0)
                + COALESCE(output_tokens, 0)
                + COALESCE(cache_creation_tokens, 0)
                + COALESCE(cache_read_tokens, 0)
                + COALESCE(reasoning_tokens, 0) > 0
              {session_scope}
            """,
            scoped_ids or [],
        ).fetchall()
        for session_row in session_level_rows:
            session_id = session_row["id"]
            exact_tokens = session_tokens.get(session_id, {})
            exact_token_total = sum(int(value or 0) for value in exact_tokens.values())
            if exact_token_total > 0:
                continue
            model = session_model_hints.get(session_id) or session_models.get(session_id, "")
            if not model:
                parent_session_id = _cursor_native_parent_session_id(str(session_row["source_ref"] or ""))
                if parent_session_id and parent_session_id != session_id:
                    model = session_model_hints.get(parent_session_id) or session_models.get(parent_session_id, "")
            if not model:
                continue
            breakdown = calculate_cost(
                {
                    "input": session_row["input_tokens"],
                    "output": session_row["output_tokens"],
                    "cache_creation": session_row["cache_creation_tokens"],
                    "cache_read": session_row["cache_read_tokens"],
                },
                model,
                pricing_table,
                aliases=aliases,
            )
            if not breakdown.resolution.matched_model_key:
                continue
            session_costs[session_id] = max(session_costs.get(session_id, 0.0), breakdown.total_cost_usd)
        for session_id, total_cost in session_costs.items():
            tokens = session_tokens.get(session_id, {})
            token_total = (
                tokens.get("input", 0)
                + tokens.get("output", 0)
                + tokens.get("cache_creation", 0)
                + tokens.get("cache_read", 0)
                + tokens.get("reasoning", 0)
            )
            if token_total <= 0 and total_cost <= 0:
                continue
            conn.execute(
                """
                UPDATE sessions
                SET
                  input_tokens = CASE WHEN ? > 0 THEN ? ELSE input_tokens END,
                  output_tokens = CASE WHEN ? > 0 THEN ? ELSE output_tokens END,
                  cache_creation_tokens = CASE WHEN ? > 0 THEN ? ELSE cache_creation_tokens END,
                  cache_read_tokens = CASE WHEN ? > 0 THEN ? ELSE cache_read_tokens END,
                  reasoning_tokens = CASE WHEN ? > 0 THEN ? ELSE reasoning_tokens END,
                  estimated_cost_usd = ?,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    token_total,
                    tokens.get("input", 0),
                    token_total,
                    tokens.get("output", 0),
                    token_total,
                    tokens.get("cache_creation", 0),
                    token_total,
                    tokens.get("cache_read", 0),
                    token_total,
                    tokens.get("reasoning", 0),
                    total_cost,
                    timestamp,
                    session_id,
                ),
            )
        conn.commit()
    finally:
        conn.row_factory = previous_row_factory


@main.command("ingest")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--otlp", "otlp_traces", type=click.Path(path_type=Path), default=None, help="Path to OTLP traces JSONL export file.")
@click.option("--spans-file", type=click.Path(path_type=Path), default=None, help="Path to local hook spans JSONL file.")
def ingest(db_path: Path, otlp_traces: Path | None, spans_file: Path | None) -> None:
    """Ingest telemetry records into raw_events."""
    source_path = otlp_traces or spans_file
    result = _ingest_into_db(db_path=db_path, otlp_traces=otlp_traces, spans_file=spans_file)
    click.echo(
        f"Ingested {source_path} -> {db_path} (inserted={result['inserted']}, skipped={result['skipped']})"
    )


@db.command("ingest-spans")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--spans-file", type=click.Path(path_type=Path), required=True, help="Path to local hook spans JSONL file.")
def db_ingest_spans(db_path: Path, spans_file: Path) -> None:
    """Ingest local hook spans JSONL into raw_events with source/hash dedupe."""
    result = _ingest_into_db(db_path=db_path, spans_file=spans_file)
    click.echo(
        f"Ingested {spans_file} -> {db_path} (inserted={result['inserted']}, skipped={result['skipped']})"
    )


@db.command("normalize")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--limit", type=int, default=None, help="Maximum pending raw_events to normalize.")
def db_normalize(db_path: Path, limit: int | None) -> None:
    """Normalize pending raw_events into canonical SQLite tables."""
    from reflect.store.migrate import migrate
    from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        changed_session_ids: set[str] = set()
        result = normalize_pending_raw_events(
            conn,
            limit=limit,
            changed_session_ids=changed_session_ids,
        )
        backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
        )
    finally:
        conn.close()
    click.echo(
        "Normalized raw_events "
        f"(processed={result['processed']}, failed={result['failed']}, skipped={result['skipped']})"
    )


@db.command("rebuild-graph")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def db_rebuild_graph(db_path: Path) -> None:
    """Rebuild graph_nodes and graph_edges from canonical SQLite tables."""
    from reflect.store.graph_normalize import rebuild_graph
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        result = rebuild_graph(conn)
    finally:
        conn.close()
    click.echo(f"Rebuilt graph (nodes={result['nodes']}, edges={result['edges']})")


@db.command("rebuild-rollups")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def db_rebuild_rollups(db_path: Path) -> None:
    """Rebuild aggregate rollup tables from canonical SQLite tables."""
    from reflect.store.migrate import migrate
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        result = rebuild_rollups(conn)
    finally:
        conn.close()
    click.echo(
        "Rebuilt rollups "
        f"(sessions={result['session_rollups']}, days={result['daily_rollups']}, tools={result['tool_rollups']})"
    )


@db.command("migrate")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def db_migrate(db_path: Path) -> None:
    """Apply pending SQLite migrations."""
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
    finally:
        conn.close()

    if not applied:
        click.echo(f"No pending migrations for {db_path}")
        return
    click.echo(f"Applied migrations to {db_path}: {', '.join(str(v) for v in applied)}")


@db.command("doctor")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def db_doctor(db_path: Path) -> None:
    """Inspect SQLite store migration, pragma, and foreign-key health."""
    from reflect.store.doctor import inspect_database
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        status = inspect_database(conn)
    finally:
        conn.close()

    click.echo(f"SQLite DB: {db_path}")
    applied = ", ".join(str(version) for version in status["applied_migrations"]) or "none"
    expected = ", ".join(str(version) for version in status["expected_migrations"]) or "none"
    click.echo(f"Migrations: applied={applied}; expected={expected}")
    if status["pending_migrations"]:
        pending = ", ".join(str(version) for version in status["pending_migrations"])
        click.echo(f"Pending migrations: {pending}")
    if status["unknown_migrations"]:
        unknown = ", ".join(str(version) for version in status["unknown_migrations"])
        click.echo(f"Unknown migrations: {unknown}")

    foreign_key_issues = status["foreign_key_issues"]
    if foreign_key_issues:
        click.echo(f"Foreign keys: {len(foreign_key_issues)} issue(s)")
    else:
        click.echo("Foreign keys: ok")

    pragmas = status["pragmas"]
    click.echo(
        "Pragmas: "
        f"foreign_keys={pragmas['foreign_keys']}, "
        f"journal_mode={pragmas['journal_mode']}, "
        f"synchronous={pragmas['synchronous']}, "
        f"wal_autocheckpoint={pragmas['wal_autocheckpoint']}, "
        f"busy_timeout={pragmas['busy_timeout']}"
    )
    if status["ok"]:
        click.echo("SQLite store health: ok")
        return

    click.echo("SQLite store health: needs attention")
    raise click.ClickException("SQLite store health checks failed")


@db.command("prune-sessions")
@click.option(
    "--older-than-days",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="Prune only invalid-start sessions with no trusted activity for this many days.",
)
@click.option(
    "--all-inactive-sessions",
    is_flag=True,
    help="Also prune inactive sessions with valid timestamps older than the cutoff.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Apply the previewed deletion.")
@click.option(
    "--backup/--no-backup",
    default=True,
    help="Create a timestamped database backup before applying.",
)
@click.option(
    "--vacuum",
    is_flag=True,
    help="Reclaim disk space after applying. Never runs during a dry run.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
)
def db_prune_sessions(
    older_than_days: int,
    all_inactive_sessions: bool,
    apply_changes: bool,
    backup: bool,
    vacuum: bool,
    as_json: bool,
    db_path: Path,
) -> None:
    """Preview or prune inactive sessions selected by the retention policy."""
    from dataclasses import asdict

    from reflect.preparation import PreparationStage, report_preparation_progress
    from reflect.store.migrate import migrate
    from reflect.store.retention import SessionPruner, SessionRetentionPolicy
    from reflect.store.sqlite import (
        SQLiteBackupProgress,
        backup_sqlite,
        connect_sqlite,
        connect_sqlite_read_only,
    )
    from reflect.terminal import TerminalPreparationProgress

    if vacuum and not apply_changes:
        raise click.UsageError("--vacuum requires --apply")
    if not db_path.exists():
        raise click.ClickException(f"SQLite store not found: {db_path}")

    backup_path: Path | None = None
    if not apply_changes:
        _require_snapshot_schema(
            db_path,
            refresh_hint=(
                f"Run `reflect db migrate --db-path {db_path}` before previewing "
                "session retention."
            ),
        )
        conn = connect_sqlite_read_only(db_path)
        try:
            result = SessionPruner(
                conn,
                SessionRetentionPolicy(
                    older_than_days=older_than_days,
                    include_valid_starts=all_inactive_sessions,
                ),
            ).run()
        finally:
            conn.close()
    else:
        def apply_pruning(progress):
            report_preparation_progress(
                progress,
                PreparationStage.OPENING_STORE,
                "Opening the local telemetry store...",
            )
            conn = connect_sqlite(db_path)
            applied_backup_path: Path | None = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                if backup:
                    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
                    applied_backup_path = db_path.with_name(
                        f"{db_path.name}.backup-{stamp}"
                    )
                    report_preparation_progress(
                        progress,
                        PreparationStage.BACKING_UP_STORE,
                        "Backing up the local telemetry store...",
                    )
                    last_backup_percent = -5

                    def update_backup(snapshot: SQLiteBackupProgress) -> None:
                        nonlocal last_backup_percent
                        percent = snapshot.percent_complete
                        if percent < 100 and percent < last_backup_percent + 5:
                            return
                        last_backup_percent = percent
                        report_preparation_progress(
                            progress,
                            PreparationStage.BACKING_UP_STORE,
                            f"Backing up the local telemetry store... {percent}% "
                            f"({snapshot.completed_pages:,}/{snapshot.total_pages:,} pages)",
                        )

                    backup_sqlite(
                        db_path,
                        applied_backup_path,
                        progress=update_backup,
                    )
                report_preparation_progress(
                    progress,
                    PreparationStage.MIGRATING_SCHEMA,
                    "Checking database migrations...",
                )
                migrate(conn, commit=False)
                report_preparation_progress(
                    progress,
                    PreparationStage.PRUNING_SESSIONS,
                    "Pruning eligible sessions and updating affected derived data...",
                )
                applied_result = SessionPruner(
                    conn,
                    SessionRetentionPolicy(
                        older_than_days=older_than_days,
                        include_valid_starts=all_inactive_sessions,
                    ),
                ).run(apply=True)
                conn.commit()
                if vacuum and applied_result.pruned_session_ids:
                    report_preparation_progress(
                        progress,
                        PreparationStage.VACUUMING_STORE,
                        "Vacuuming the local telemetry store...",
                    )
                    conn.execute("VACUUM")
                report_preparation_progress(
                    progress,
                    PreparationStage.COMPLETE,
                    "Session pruning complete.",
                )
                return applied_result, applied_backup_path
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
            finally:
                conn.close()

        result, backup_path = TerminalPreparationProgress().run(
            apply_pruning,
            initial_message="Preparing session pruning...",
        )

    payload = {
        "dry_run": result.dry_run,
        "older_than_days": older_than_days,
        "all_inactive_sessions": all_inactive_sessions,
        "candidate_count": len(result.candidates),
        "candidates": [asdict(candidate) for candidate in result.candidates],
        "pruned_session_ids": list(result.pruned_session_ids),
        "backup_path": str(backup_path) if backup_path else None,
        "vacuumed": bool(vacuum and result.pruned_session_ids),
        "foreign_key_violations": [list(row) for row in result.foreign_key_violations],
        "graph": result.graph,
        "rollups": result.rollups,
    }
    if as_json:
        _echo_json(payload)
        return
    action = "Pruned" if apply_changes else "Would prune"
    session_scope = "inactive" if all_inactive_sessions else "invalid-start"
    click.echo(
        f"{action} {len(result.candidates)} {session_scope} session(s) "
        f"with no trusted activity for {older_than_days} days."
    )
    for candidate in result.candidates:
        click.echo(
            f"  {candidate.session_id} · {candidate.agent or 'unknown'} · "
            f"{candidate.last_observed_at or 'no trusted activity'} · "
            f"{sum(candidate.dependent_rows.values())} dependent row(s)"
        )
    if backup_path:
        click.echo(f"Backup: {backup_path}")
    if not apply_changes and result.candidates:
        click.echo("Dry run only. Re-run with --apply to prune these exact policy matches.")


@main.group()
def schema() -> None:
    """Schema and model tooling."""


@schema.command("export")
@click.option("--output", type=click.Path(path_type=Path), required=True)
def schema_export(output: Path) -> None:
    """Export Pydantic JSON Schema for core models."""
    from reflect.schema.events import RawEvent

    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {"$schema": "https://json-schema.org/draft/2020-12/schema", "definitions": {"RawEvent": RawEvent.model_json_schema()}}
    output.write_text(_json_stdlib.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"Wrote schema to {output}")


if __name__ == "__main__":
    main()
