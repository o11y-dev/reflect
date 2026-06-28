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
        --otlp-traces ~/.reflect/state/otlp/otel-traces.json --no-terminal

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
from datetime import UTC, datetime
from importlib import metadata as importlib_metadata
from importlib import resources as importlib_resources
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

# ---------------------------------------------------------------------------
# Reflect home directory
# ---------------------------------------------------------------------------

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
HOOK_HOME = Path(os.environ.get("IDE_OTEL_HOOK_HOME",
                                 Path.home() / ".local" / "share" / "opentelemetry-hooks"))

# ---------------------------------------------------------------------------
# Re-exports from split modules — keeps backward compatibility for legacy serve.py,
# tests, and any external consumers that import from reflect.core.
# ---------------------------------------------------------------------------

from reflect.dashboard import (  # noqa: F401
    _artifact_report_ref,
    _build_dashboard_json,
    _sql_dashboard_payload,
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
    _copy_config_snapshot as _instrumentation_copy_config_snapshot,
)
from reflect.instrumentation import (
    _reflect_agent_dir as _instrumentation_reflect_agent_dir,
)
from reflect.instrumentation import (
    _run_setup as _instrumentation_run_setup,
)
from reflect.instrumentation import (
    _snapshot_detected_agent_configs as _instrumentation_snapshot_detected_agent_configs,
)
from reflect.models import AgentStats, TelemetryStats  # noqa: F401
from reflect.parsing import (  # noqa: F401
    _canonical_otlp_traces_path,
    _discover_rich_session_files,
    _extract_session_id,
    _flatten_otlp_attributes,
    _flatten_text_content,
    _infer_otlp_logs_file,
    _iter_claude_log_spans,
    _iter_claude_session_spans,
    _iter_codex_log_spans,
    _iter_codex_session_spans,
    _iter_copilot_session_spans,
    _iter_cursor_session_spans,
    _iter_gemini_session_spans,
    _load_json_lines,
    _load_otlp_logs,
    _load_otlp_traces,
)
from reflect.processing import _process_span, analyze_telemetry  # noqa: F401
from reflect.report import render_report  # noqa: F401
from reflect.skill_extraction import (  # noqa: F401
    _build_graph_evidence,
    _build_skill_evidence_bundle,
    _build_skill_evidence_bundle_from_sql,
    _build_skills_extraction_prompt,
    _build_skills_extraction_prompt_from_bundle,
    _compress_tool_sequence,
    _extract_recovery_chains,
    _load_extracted_skills,
    _serialize_sessions_for_skills,
    _strip_json_fences,
)
from reflect.store.provenance import HOOK_ORIGINS, NATIVE_OTLP_ORIGINS
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
        "local_skill_path": ".claude/skills/",
        "hook_agent": "claude",
        "global_path": "~/.claude/skills/",
        "recommendation": "Run reflect setup to wire Claude hooks and enable native Claude telemetry.",
    },
    {
        "name": "Cursor",
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
        "env": "CODEX_HOME",
        "default": lambda: Path.home() / ".codex",
        "path_kind": "home",
        "local_skill_path": ".codex/skills/",
        "hook_agent": "codex",
        "global_path": "~/.codex/skills/",
        "recommendation": "Use native Codex OTel for interactive runs; reflect does not yet ship a native session adapter for Codex logs.",
    },
    {
        "name": "Windsurf",
        "env": "WINDSURF_HOME",
        "default": lambda: Path.home() / ".codeium" / "windsurf",
        "path_kind": "home",
        "local_skill_path": ".windsurf/skills/",
        "global_path": "~/.codeium/windsurf/skills/",
        "recommendation": "Native OTel and hooks still need verification for Windsurf.",
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
        "env": "ANTIGRAVITY_HOME",
        "default": lambda: Path.home() / ".gemini" / "antigravity",
        "path_kind": "home",
        "local_skill_path": ".agents/skills/",
        "global_path": "~/.gemini/antigravity/skills/",
        "recommendation": "Core target for reflect telemetry and skill distribution.",
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
_DOCTOR_MATRIX_PLANNED = {"Antigravity", "OpenClaw"}


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
    default=None,
    help="Deprecated. Use --terminal for the legacy Rich terminal view or --no-terminal for the legacy markdown report.",
)
@click.option(
    "--dashboard-artifact",
    type=click.Path(path_type=Path),
    default=None,
    help="Deprecated. Write the legacy dashboard JSON artifact to a file.",
)
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used for SQL-backed browser report endpoints.",
)
@click.option(
    "--sql-only",
    is_flag=True,
    help="Deprecated no-op; SQLite-backed report data is now the default.",
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
    terminal: bool | None,
    dashboard_artifact: Path | None,
    db_path: Path,
    sql_only: bool,
    demo: bool,
    time_range: str,
) -> None:
    """Open the local Reflect browser report."""
    if ctx.invoked_subcommand is not None:
        return

    if sql_only:
        click.echo("Note: --sql-only is deprecated; SQLite-backed report data is now the default.")

    if terminal is None:
        _run_browser_report(
            otlp_traces=otlp_traces,
            sessions_dir=sessions_dir,
            spans_dir=spans_dir,
            time_range=time_range,
            demo=demo,
            dashboard_artifact=dashboard_artifact,
            output=output,
            db_path=db_path,
        )
        return

    click.echo(
        "Note: terminal and markdown modes are deprecated. Run `reflect` with no terminal flags to open the browser report."
    )

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
        click.echo("Note: --dashboard-artifact is deprecated; the browser report is served from SQLite by default.")
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


def _run_browser_report(
    *,
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    dashboard_artifact: Path | None,
    output: Path | None,
    db_path: Path,
) -> None:
    from collections import Counter

    console = Console()
    update_notice = _build_startup_update_notice()
    if update_notice:
        click.echo(f"reflect notice: {update_notice}")

    include_native_sessions = False
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
    with console.status("[bold orange3]reflecting...[/bold orange3]", spinner="dots"):
        preparation = _prepare_sql_report_db(
            db_path,
            otlp_traces=otlp_traces,
            include_native_sessions=include_native_sessions,
        )
    ingest_sources = preparation.get("ingest_sources") or {}
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    summary.add_row("Inserted", f"{preparation['ingest']['inserted']:,}")
    summary.add_row("Skipped", f"{preparation['ingest']['skipped']:,}")
    summary.add_row("Normalized", f"{preparation['normalize']['processed']:,}")
    summary.add_row("Sessions", f"{preparation['rollups']['session_rollups']:,}")
    if ingest_sources:
        summary.add_row("", "")
        for name, result in ingest_sources.items():
            source_type = str(result.get("source_type") or "")
            native_events = sum(
                int(counts.get("native_events") or 0)
                for counts in (result.get("agents") or {}).values()
            )
            hook_events = sum(
                int(counts.get("hook_events") or 0)
                for counts in (result.get("agents") or {}).values()
            )
            source_detail = f"{result['inserted']:,} inserted / {result['skipped']:,} skipped"
            if source_type in {"otlp_traces_json", "otlp_logs_json"}:
                source_detail += f" / {native_events:,} native / {hook_events:,} hook event(s)"
            elif hook_events:
                source_detail += f" / {hook_events:,} hook event(s)"
            summary.add_row(
                name.replace("_", " ").title(),
                source_detail,
            )
            for agent, counts in sorted((result.get("agents") or {}).items()):
                agent_detail = f"{counts['events']:,} event(s)"
                if source_type in {"otlp_traces_json", "otlp_logs_json"}:
                    agent_detail += (
                        f" / {int(counts.get('native_events') or 0):,} native"
                        f" / {int(counts.get('hook_events') or 0):,} hook"
                    )
                elif counts.get("hook_events"):
                    agent_detail += f"{' / ' if agent_detail else ''}{counts['hook_events']:,} hook"
                summary.add_row(
                    f"  {agent}",
                    agent_detail,
                )
    console.print(Panel(summary, title="[bold orange3]REFLECT[/bold orange3]", border_style="orange3"))

    stats = TelemetryStats(
        session_files=0,
        span_files=0,
        total_events=0,
        events_by_type=Counter(),
        events_by_file={},
    )
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
    if dashboard_artifact is not None:
        click.echo("Note: --dashboard-artifact is deprecated; the browser report is served from SQLite by default.")
        dashboard_artifact.parent.mkdir(parents=True, exist_ok=True)
        dashboard_artifact.write_text(
            _json_stdlib.dumps(_sql_dashboard_payload(db_path)),
            encoding="utf-8",
        )
    _start_publish_server(stats, db_path=db_path, sql_only=False)


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
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used for SQL-backed browser report endpoints.",
)
@click.option(
    "--sql-only",
    is_flag=True,
    help="Deprecated no-op; SQLite-backed report data is now the default.",
)
def report(
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    dashboard_artifact: Path | None,
    output: Path | None,
    db_path: Path,
    sql_only: bool,
) -> None:
    """Deprecated alias for `reflect`."""
    click.echo("Note: `reflect report` is deprecated. Run `reflect` to open the browser report.")
    if sql_only:
        click.echo("Note: --sql-only is deprecated; SQLite-backed report data is now the default.")
    _run_browser_report(
        otlp_traces=otlp_traces,
        sessions_dir=sessions_dir,
        spans_dir=spans_dir,
        time_range=time_range,
        demo=demo,
        dashboard_artifact=dashboard_artifact,
        output=output,
        db_path=db_path,
    )


# ---------------------------------------------------------------------------
# Skills command
# ---------------------------------------------------------------------------

# Known agent CLIs with their non-interactive (print-mode) flags.
# First entry in the list is the auto-detection priority order.
_SKILL_AGENT_SPECS: list[tuple[str, list[str]]] = [
    ("claude", ["--print"]),
    ("gemini", ["-p"]),
    ("codex", ["exec"]),
    ("cursor-agent", ["--print", "--trust", "--mode", "ask"]),
    ("copilot", ["--prompt"]),
    ("opencode", ["run"]),
    ("qwen", ["--print"]),
]
_SKILL_AGENT_NAMES = ", ".join(name for name, _ in _SKILL_AGENT_SPECS)
_AGENT_ERROR_LIMIT = 2000


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
        for name, flags in _SKILL_AGENT_SPECS:
            if name == agent:
                return agent, flags
        # Unknown agent — fall back to --print and hope for the best
        return agent, ["--print"]

    # Probe all supported CLIs
    available = [
        (name, flags)
        for name, flags in _SKILL_AGENT_SPECS
        if shutil.which(name) and _skill_agent_ready(name)
    ]

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
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=REFLECT_HOME / "state" / "reflect.db",
    help="SQLite store used to derive SQL graph evidence for skill extraction.",
)
def skills(
    otlp_traces: Path | None,
    sessions_dir: Path | None,
    spans_dir: Path | None,
    time_range: str,
    demo: bool,
    agent: str | None,
    yes: bool,
    db_path: Path,
) -> None:
    """Extract reusable skills from your AI sessions using an agent."""
    import json as _json
    import subprocess
    import tempfile

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

        from reflect.store.sqlite import connect_sqlite

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

        _prepare_sql_report_db(
            db_path,
            otlp_traces=sql_otlp_traces,
            include_native_sessions=include_native_sessions,
        )
        conn = connect_sqlite(db_path)
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
        result = subprocess.run([agent_bin, *agent_flags, prompt], capture_output=True, text=True)
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

    detected = [a for a in _detect_agents() if a["detected"]]
    install_agents = _select_skill_install_agents(detected, console, yes=yes)
    if not yes:
        console.print()
        confirmed = click.confirm(
            f"Write {len(selected)} skill(s) to {len(install_agents)} selected agent(s)?",
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
        for agent_spec in install_agents:
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
    if sql_bundle_used:
        console.print("[dim]Included SQL stats + Behavioral Memory Graph evidence in extraction prompt.[/dim]")
    elif graph_evidence_attached:
        console.print("[dim]Included SQL Behavioral Memory Graph evidence in extraction prompt.[/dim]")
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


def _agent_key(name: str) -> str:
    return name.lower().replace(" ", "-")


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
    """Distribute the reflect and opentelemetry skills to detected agents."""
    # Bundle reflect core skills for automatic setup distribution.
    bundled_skills_dir = Path(__file__).parent / "data" / "skills"

    available_skills: dict[str, Path] = {}

    reflect_skill = bundled_skills_dir / "reflect"
    if (reflect_skill / "SKILL.md").exists():
        available_skills["reflect"] = reflect_skill

    reflect_skills_helper = bundled_skills_dir / "reflect-skills"
    if (reflect_skills_helper / "SKILL.md").exists():
        available_skills["reflect-skills"] = reflect_skills_helper

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



def _reflect_agent_dir(agent_name: str) -> Path:
    return _instrumentation_reflect_agent_dir(REFLECT_HOME, agent_name)


def _copy_config_snapshot(agent_name: str, source: Path) -> Path:
    return _instrumentation_copy_config_snapshot(REFLECT_HOME, agent_name, source)


def _snapshot_detected_agent_configs(console, agents: list[dict]) -> None:
    _instrumentation_snapshot_detected_agent_configs(console, agents, reflect_home=REFLECT_HOME)


def _run_setup(
    console,
    *,
    capture_text: bool | None = None,
    mask_captured_text: bool = True,
    text_max_chars: int | None = None,
    selected_agent_names: set[str] | None = None,
    local_agent_names: set[str] | None = None,
) -> None:
    _instrumentation_run_setup(
        console,
        reflect_home=REFLECT_HOME,
        hook_home=HOOK_HOME,
        detect_agents=_detect_agents,
        distribute_skills=_distribute_skills,
        capture_text=capture_text,
        mask_captured_text=mask_captured_text,
        text_max_chars=text_max_chars,
        selected_agent_names=selected_agent_names,
        local_agent_names=local_agent_names,
    )


def _resolve_setup_agent_selection(
    console,
    *,
    agent_names: tuple[str, ...],
    all_agents: bool,
) -> set[str] | None:
    detected = [agent for agent in _detect_agents() if agent.get("detected")]
    detected_keys = {_agent_key(str(agent["name"])) for agent in detected}
    if agent_names:
        selected = {_agent_key(name) for name in agent_names}
        unknown = selected - detected_keys
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
)
def setup(
    capture_text: bool | None,
    text_capture_mode: str | None,
    mask_captured_text: bool,
    text_max_chars: int | None,
    agent_names: tuple[str, ...],
    all_agents: bool,
    local_agent_names: tuple[str, ...],
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
    local_agent_keys = {_agent_key(name) for name in local_agent_names}
    unknown_local = local_agent_keys - selected_agent_keys if selected_agent_keys is not None else set()
    if unknown_local:
        raise click.ClickException(
            "--local-agent must also be selected with --agent: " + ", ".join(sorted(unknown_local))
        )
    _run_setup(
        console,
        capture_text=capture_text,
        mask_captured_text=mask_captured_text,
        text_max_chars=text_max_chars,
        selected_agent_names=selected_agent_keys,
        local_agent_names=local_agent_keys,
    )


@main.group("doctor", invoke_without_command=True)
@click.pass_context
def doctor(ctx) -> None:
    """Inspect local reflect health, or run focused doctor checks."""
    if ctx.invoked_subcommand is None:
        _run_doctor()


def _run_doctor() -> None:
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
    try:
        gw_pid = _gw_running()
    except PermissionError:
        gw_pid = None
    summary.add_row(
        "otlp gateway",
        _status_markup(gw_pid is not None, present=f"running (PID {gw_pid})", missing="stopped"),
    )
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
            alias_result = _ensure_sql_costs(conn, alias_path=alias_path)
            rebuild_rollups(conn)
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


def _pipx_upgrade_package(pipx: str, package: str) -> None:
    subprocess.check_call([pipx, "upgrade", package])


@main.command()
@click.option(
    "--apply",
    is_flag=True,
    help="Attempt package upgrades via pipx when a newer reflect release is available.",
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
            console.print("Install pipx, then run [bold]pipx upgrade o11y-reflect[/] and [bold]pipx upgrade opentelemetry-hooks[/].")
            raise SystemExit(1)
        try:
            for package in ("o11y-reflect", "opentelemetry-hooks"):
                console.print(f"Upgrading [bold]{package}[/]...")
                _pipx_upgrade_package(pipx, package)
            console.print("[green]Package upgrades finished.[/] Re-run [bold]reflect doctor[/] to refresh the cached status.")
        except subprocess.CalledProcessError as exc:
            console.print(f"[red]pipx upgrade failed:[/] {exc}")
            raise SystemExit(exc.returncode or 1) from exc

        if advisor["local_issues"]:
            console.print("Local drift remains. Run [bold]reflect setup[/] to refresh global hooks and skill copies.")
    else:
        if release["update_available"]:
            console.print("Use [bold]reflect update --apply[/] to upgrade reflect and opentelemetry-hooks.")
        else:
            console.print("[green]No newer reflect release is available right now.[/]")
            console.print("Use [bold]reflect update --apply[/] to force-check pipx upgrades for reflect and opentelemetry-hooks.")
        if advisor["local_issues"]:
            console.print("For local hook or skill drift, run [bold]reflect setup[/] to refresh global wiring.")
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


@main.group()
def db() -> None:
    """SQLite store management commands."""


def _ingest_into_db(
    *,
    db_path: Path,
    otlp_traces: Path | None = None,
    spans_file: Path | None = None,
) -> dict[str, int]:
    from reflect.store.ingest import ingest_local_spans_file, ingest_otlp_traces_file
    from reflect.store.migrate import migrate
    from reflect.store.normalize import normalize_pending_raw_events
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite

    if (otlp_traces is None) == (spans_file is None):
        raise click.ClickException("Pass exactly one of --otlp or --spans-file")

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        if otlp_traces is not None:
            result = ingest_otlp_traces_file(conn, file_path=otlp_traces)
        else:
            result = ingest_local_spans_file(conn, file_path=spans_file)
        normalize_pending_raw_events(conn)
        _ensure_sql_costs(conn)
        rebuild_rollups(conn)
    finally:
        conn.close()
    return result


def _ensure_sql_costs(conn, *, alias_path: Path | None = None):
    from reflect.cost_aliases import ensure_cost_aliases

    alias_result = ensure_cost_aliases(conn, alias_path=alias_path)
    _reprice_sql_store(conn, alias_path=alias_result.alias_path)
    return alias_result


def _cursor_native_parent_session_id(source_ref: str) -> str:
    match = re.search(r"/agent-transcripts/([^/]+)/", source_ref or "")
    return match.group(1) if match else ""


def _prepare_sql_report_db(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool = False,
) -> dict[str, object]:
    from reflect.store.cursor_adapter import apply_cursor_transcript_usage_estimates
    from reflect.store.graph_normalize import rebuild_graph
    from reflect.store.ingest import (
        ingest_native_session_file,
        ingest_otlp_logs_file,
        ingest_otlp_traces_file,
    )
    from reflect.store.migrate import migrate
    from reflect.store.normalize import normalize_pending_raw_events, repair_telemetry_provenance
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        applied = migrate(conn)
        ingest_result = {"inserted": 0, "skipped": 0}
        ingest_sources: dict[str, dict[str, object]] = {}
        source_refs: dict[str, list[str]] = {}
        source_types: dict[str, str] = {}
        cursor_native_files: list[Path] = []
        if otlp_traces is not None and otlp_traces.exists():
            traces_result = ingest_otlp_traces_file(conn, file_path=otlp_traces)
            ingest_sources["otlp_traces"] = traces_result
            ingest_sources["otlp_traces"]["source_type"] = "otlp_traces_json"
            source_refs["otlp_traces"] = [str(otlp_traces)]
            source_types["otlp_traces"] = "otlp_traces_json"
            ingest_result["inserted"] += traces_result["inserted"]
            ingest_result["skipped"] += traces_result["skipped"]
            otlp_logs = _infer_otlp_logs_file(otlp_traces)
            if otlp_logs is not None and otlp_logs.exists():
                logs_result = ingest_otlp_logs_file(conn, file_path=otlp_logs)
                ingest_sources["otlp_logs"] = logs_result
                ingest_sources["otlp_logs"]["source_type"] = "otlp_logs_json"
                source_refs["otlp_logs"] = [str(otlp_logs)]
                source_types["otlp_logs"] = "otlp_logs_json"
                ingest_result["inserted"] += logs_result["inserted"]
                ingest_result["skipped"] += logs_result["skipped"]
        if include_native_sessions:
            native_result = {"inserted": 0, "skipped": 0}
            native_refs: list[str] = []
            for agent, session_file in _discover_rich_session_files():
                source_ref = f"native_session:{agent}:{session_file}"
                native_refs.append(source_ref)
                if agent == "cursor":
                    cursor_native_files.append(session_file)
                result = ingest_native_session_file(
                    conn,
                    file_path=session_file,
                    agent=agent,
                    source_id=source_ref,
                    skip_existing_sessions=True,
                )
                native_result["inserted"] += result["inserted"]
                native_result["skipped"] += result["skipped"]
            if native_result["inserted"] or native_result["skipped"]:
                ingest_sources["native_sessions"] = native_result
                ingest_sources["native_sessions"]["source_type"] = "native_session"
                source_refs["native_sessions"] = native_refs
                source_types["native_sessions"] = "native_session"
                ingest_result["inserted"] += native_result["inserted"]
                ingest_result["skipped"] += native_result["skipped"]
        repair_telemetry_provenance(conn)
        for name, refs in source_refs.items():
            if name in ingest_sources:
                source_type = source_types.get(name, "")
                ingest_sources[name]["agents"] = _raw_event_agent_breakdown(
                    conn, source_ids_by_type={source_type: refs} if source_type else {}
                )
        normalize_result = normalize_pending_raw_events(conn)
        cursor_adapter_result = (
            apply_cursor_transcript_usage_estimates(conn, cursor_native_files)
            if cursor_native_files
            else {"updated": 0, "skipped": 0, "missing": 0}
        )
        _ensure_sql_costs(conn)
        graph_result = rebuild_graph(conn)
        rollup_result = rebuild_rollups(conn)
    finally:
        conn.close()
    return {
        "applied_migrations": applied,
        "ingest": ingest_result,
        "ingest_sources": ingest_sources,
        "normalize": normalize_result,
        "cursor_adapter": cursor_adapter_result,
        "graph": graph_result,
        "rollups": rollup_result,
    }


def _raw_event_agent_breakdown(conn, *, source_ids_by_type: dict[str, list[str]]) -> dict[str, dict[str, int]]:
    """Break down raw events by agent using durable provenance, not hook-shaped attrs."""
    if not source_ids_by_type:
        return {}
    totals: dict[str, dict[str, int]] = {}
    native_otlp_placeholders = ", ".join("?" for _ in NATIVE_OTLP_ORIGINS)
    hook_placeholders = ", ".join("?" for _ in HOOK_ORIGINS)
    all_source_ids = [sid for ids in source_ids_by_type.values() for sid in ids]
    for offset in range(0, len(all_source_ids), 500):
        chunk = all_source_ids[offset:offset + 500]
        placeholders = ", ".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
              COALESCE(
                NULLIF(json_extract(attrs_json, '$."gen_ai.client.name"'), ''),
                NULLIF(json_extract(attrs_json, '$."agent.name"'), ''),
                NULLIF(json_extract(attrs_json, '$."service.name"'), ''),
                'unknown'
              ) AS agent,
              COUNT(*) AS events,
              SUM(CASE WHEN origin_kind IN ({native_otlp_placeholders}) THEN 1 ELSE 0 END) AS native_events,
              SUM(CASE WHEN origin_kind IN ({hook_placeholders}) THEN 1 ELSE 0 END) AS hook_events
            FROM raw_events
            WHERE source_id IN ({placeholders})
            GROUP BY agent
            ORDER BY events DESC, agent ASC
            """,
            [*NATIVE_OTLP_ORIGINS, *HOOK_ORIGINS, *chunk],
        ).fetchall()
        for row in rows:
            agent = str(row[0] or "unknown")
            counts = totals.setdefault(agent, {"events": 0, "hook_events": 0, "native_events": 0})
            counts["events"] += int(row[1] or 0)
            counts["native_events"] += int(row[2] or 0)
            counts["hook_events"] += int(row[3] or 0)
    return dict(
        sorted(totals.items(), key=lambda item: (-item[1]["events"], item[0]))
    )


def _reprice_sql_store(conn, *, alias_path: Path | None = None) -> None:
    from reflect.config import load_model_aliases
    from reflect.pricing import calculate_cost, load_pricing_table

    pricing_table = load_pricing_table()
    aliases = load_model_aliases(alias_path)
    import sqlite3

    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
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
            """
        ).fetchall()
        model_rows = conn.execute(
            """
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
            GROUP BY session_id, model
            ORDER BY session_id ASC, count DESC
            """
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
        session_level_rows = conn.execute(
            """
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
            """
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


@db.command("ingest-otlp")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option("--otlp-traces", type=click.Path(path_type=Path), required=True, help="Path to OTLP traces JSONL export file.")
def db_ingest(db_path: Path, otlp_traces: Path) -> None:
    """Ingest OTLP traces JSONL into raw_events with source/hash dedupe (legacy alias)."""
    result = _ingest_into_db(db_path=db_path, otlp_traces=otlp_traces)
    click.echo(
        f"Ingested {otlp_traces} -> {db_path} (inserted={result['inserted']}, skipped={result['skipped']})"
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
    from reflect.store.normalize import normalize_pending_raw_events
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        result = normalize_pending_raw_events(conn, limit=limit)
    finally:
        conn.close()
    click.echo(
        "Normalized raw_events "
        f"(processed={result['processed']}, failed={result['failed']}, skipped={result['skipped']})"
    )


@db.command("sync-instructions")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path),
    default=None,
    help="Workspace root to scan for instruction files.",
)
def db_sync_instructions(db_path: Path, workspace_root: Path | None) -> None:
    """Discover AGENTS.md, CLAUDE.md, GEMINI.md, and similar instruction files into memories."""
    from reflect.store.instruction_memory import upsert_instruction_memories
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite

    workspace_root = workspace_root or Path.cwd()
    conn = connect_sqlite(db_path)
    try:
        migrate(conn)
        result = upsert_instruction_memories(conn, workspace_root=workspace_root, home_root=Path.home())
    finally:
        conn.close()
    click.echo(
        "Synced instruction memories "
        f"(discovered={result['discovered']}, inserted={result['inserted']}, updated={result['updated']})"
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
