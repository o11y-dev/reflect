from __future__ import annotations

import json as _json_stdlib
import re
import shutil
import subprocess
import tomllib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from reflect.parsing import _canonical_otlp_traces_path
from reflect.utils import _json_loads

_HOOK_PACKAGE_SPEC = "opentelemetry-hooks==0.11.0"
_HOOK_CFG_ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"
_HOOK_CFG_ENDPOINT_DEFAULT = "http://localhost:4317"
_HOOK_CFG_PROTOCOL_KEY = "OTEL_EXPORTER_OTLP_PROTOCOL"
_HOOK_CFG_PROTOCOL_DEFAULT = "grpc"


def _agent_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _reflect_agent_dir(reflect_home: Path, agent_name: str) -> Path:
    return reflect_home / "agents" / _agent_slug(agent_name)


def _copy_config_snapshot(reflect_home: Path, agent_name: str, source: Path) -> Path:
    dest_dir = _reflect_agent_dir(reflect_home, agent_name) / "config-snapshots"
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


def _snapshot_detected_agent_configs(console, agents: list[dict], *, reflect_home: Path) -> None:
    for agent in agents:
        snapshots = []
        for source in _agent_config_candidates(agent):
            try:
                snapshots.append(_copy_config_snapshot(reflect_home, agent["name"], source))
            except Exception as exc:
                console.print(f"  [red]✗[/] Failed to snapshot {agent['name']} config {source}: {exc}")
        for snapshot in snapshots:
            console.print(f"  [green]✓[/] Saved {agent['name']} config snapshot → {snapshot}")


def _configure_claude_native_otel(console, hook_config: dict[str, str]) -> None:
    settings_path = Path.home() / ".claude" / "settings.json"
    desired_env = _claude_native_otel_env(hook_config)

    if settings_path.exists():
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]✗[/] Failed to read Claude Code settings {settings_path}: {exc}")
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
        console.print(f"  [green]✓[/] Enabled native Claude Code OTel in {settings_path}")
    else:
        console.print(f"  [green]✓[/] Native Claude Code OTel already enabled in {settings_path}")


def _configure_copilot_native_otel(console, hook_config: dict[str, str]) -> None:
    desired = _copilot_native_otel_settings(hook_config)

    searched_paths = _agent_config_paths({"name": "GitHub Copilot"})
    updated_any = False
    for settings_path in [path for path in searched_paths if path.exists()]:
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]✗[/] Failed to read Copilot settings {settings_path}: {exc}")
            continue

        changed = False
        for key, value in desired.items():
            if settings.get(key) != value:
                settings[key] = value
                changed = True

        if changed:
            settings_path.write_text(_json_stdlib.dumps(settings, indent=2) + "\n")
            console.print(f"  [green]✓[/] Enabled native Copilot OTel in {settings_path}")
            updated_any = True
        else:
            console.print(f"  [green]✓[/] Native Copilot OTel already enabled in {settings_path}")
            updated_any = True

    if not updated_any:
        console.print("  [yellow]•[/] Skipped native Copilot OTel: no VS Code settings.json file was found.")
        for path in searched_paths:
            console.print(f"    [dim]- {path}[/]")


def _configure_gemini_native_otel(console, hook_config: dict[str, str]) -> None:
    settings_path = Path.home() / ".gemini" / "settings.json"
    if not settings_path.exists():
        console.print("  [dim]•[/] No Gemini CLI settings file detected; kept env guidance only.")
        return

    try:
        settings = _json_loads(settings_path.read_text())
    except Exception as exc:
        console.print(f"  [red]✗[/] Failed to read Gemini CLI settings {settings_path}: {exc}")
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
        console.print(f"  [green]✓[/] Enabled native Gemini telemetry in {settings_path}")
    else:
        console.print(f"  [green]✓[/] Native Gemini telemetry already enabled in {settings_path}")


def _configure_copilot_cli_native_otel(console, hook_config: dict[str, str]) -> None:
    """Set Copilot CLI OTel env vars in VS Code settings.json env block."""
    desired_env = _copilot_cli_native_otel_env(hook_config)

    searched_paths = _agent_config_paths({"name": "GitHub Copilot"})
    updated_any = False
    for settings_path in [path for path in searched_paths if path.exists()]:
        try:
            settings = _json_loads(settings_path.read_text())
        except Exception as exc:
            console.print(f"  [red]✗[/] Failed to read Copilot settings {settings_path}: {exc}")
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
            console.print(f"  [green]✓[/] Enabled Copilot CLI OTel env vars in {settings_path}")
        else:
            console.print(f"  [green]✓[/] Copilot CLI OTel env vars already set in {settings_path}")
        updated_any = True

    if not updated_any:
        console.print("  [yellow]•[/] Skipped Copilot CLI OTel env vars: no VS Code settings.json file was found.")
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
            console.print(f"  [red]✗[/] Failed to read Codex config {config_path}: {exc}")
            return
        existing_otel = existing.get("otel")
        if _codex_native_otel_matches_desired(existing_otel, desired_otel):
            console.print(f"  [green]✓[/] Native Codex OTel already enabled in {config_path}")
            return
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)

    original = config_path.read_text() if config_path.exists() else ""
    otel_block = _render_codex_native_otel_block(hook_config)
    updated = _upsert_toml_section(original, "otel", otel_block)

    config_path.write_text(updated)
    console.print(f"  [green]✓[/] Enabled native Codex OTel in {config_path}")


def _native_status_markup(status: str) -> str:
    if status == "ready":
        return "[green]ready[/]"
    if status == "incomplete":
        return "[yellow]incomplete[/]"
    return "[red]missing[/]" if status == "missing" else "[red]unreadable[/]"


def _render_native_otel_panel(console, hook_runtime_config: dict[str, str]) -> None:
    from rich import box
    from rich.panel import Panel
    from rich.table import Table

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


def _run_setup(
    console,
    *,
    reflect_home: Path,
    hook_home: Path,
    detect_agents: Callable[[], list[dict]],
    distribute_skills: Callable[[object], None],
) -> None:
    console.print("\n[bold cyan]reflect setup[/]\n")
    console.print("[dim]Prepare local telemetry capture, wire supported agents, and leave clear next steps.[/]")

    console.print("\n[bold]Step 1: Prepare reflect home[/]")
    for subdir in ("state", "state/local_spans", "state/sessions", "reports", "agents"):
        (reflect_home / subdir).mkdir(parents=True, exist_ok=True)
    console.print(f"  [green]✓[/] Created [bold]{reflect_home}[/]")

    detected_agents = [agent for agent in detect_agents() if agent["detected"]]
    if detected_agents:
        console.print("\n[bold]Step 2: Snapshot detected agent configs[/]")
        _snapshot_detected_agent_configs(console, detected_agents, reflect_home=reflect_home)

    console.print("\n[bold]Step 3: Install or verify opentelemetry-hooks[/]")
    otel_hook = shutil.which("otel-hook")
    if otel_hook:
        console.print(f"  [green]✓[/] opentelemetry-hooks already installed ({otel_hook})")
    else:
        console.print("  [yellow]•[/] Installing opentelemetry-hooks via pipx...")
        try:
            subprocess.check_call(
                ["pipx", "install", _HOOK_PACKAGE_SPEC],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            otel_hook = shutil.which("otel-hook")
            console.print(f"  [green]✓[/] Installed opentelemetry-hooks ({otel_hook})")
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            console.print(f"  [red]✗[/] Failed to install opentelemetry-hooks: {exc}")
            console.print(f"    Install manually: [bold]pipx install {_HOOK_PACKAGE_SPEC}[/]")

    console.print("\n[bold]Step 4: Configure local telemetry export[/]")
    config_path = hook_home / "otel_config.json"
    if config_path.exists():
        backup = _copy_config_snapshot(reflect_home, "opentelemetry-hooks", config_path)
        console.print(f"  [green]✓[/] Saved hook config snapshot → {backup}")
        config = _json_loads(config_path.read_text())
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        example_path = hook_home / "otel_config.example.json"
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
    console.print(f"  [green]✓[/] Hook config updated ({config_path})")

    hook_spans_dir = hook_home / ".state" / "local_spans"
    reflect_spans_dir = reflect_home / "state" / "local_spans"
    if hook_spans_dir.resolve() != reflect_spans_dir.resolve():
        if reflect_spans_dir.is_dir() and not reflect_spans_dir.is_symlink() and not any(reflect_spans_dir.iterdir()):
            reflect_spans_dir.rmdir()
        if not reflect_spans_dir.exists():
            hook_spans_dir.mkdir(parents=True, exist_ok=True)
            reflect_spans_dir.symlink_to(hook_spans_dir)
            console.print(f"  [green]✓[/] Linked local_spans → {hook_spans_dir}")

    hook_sessions_dir = hook_home / ".state" / "sessions"
    reflect_sessions_dir = reflect_home / "state" / "sessions"
    if hook_sessions_dir.resolve() != reflect_sessions_dir.resolve():
        if reflect_sessions_dir.is_dir() and not reflect_sessions_dir.is_symlink() and not any(reflect_sessions_dir.iterdir()):
            reflect_sessions_dir.rmdir()
        if not reflect_sessions_dir.exists():
            hook_sessions_dir.mkdir(parents=True, exist_ok=True)
            reflect_sessions_dir.symlink_to(hook_sessions_dir)
            console.print(f"  [green]✓[/] Linked sessions → {hook_sessions_dir}")

    ws_traces_otlp = Path.cwd() / "reflect" / "state" / "otlp" / "otel-traces.json"
    ws_traces_root = Path.cwd() / "reflect" / "state" / "otel-traces.json"
    ws_traces = ws_traces_otlp if ws_traces_otlp.exists() else ws_traces_root

    home_traces = _canonical_otlp_traces_path()
    home_traces.parent.mkdir(parents=True, exist_ok=True)
    if ws_traces.exists() and not home_traces.exists():
        home_traces.symlink_to(ws_traces)
        console.print(f"  [green]✓[/] Linked workspace traces → {ws_traces}")

    ws_logs_otlp = Path.cwd() / "reflect" / "state" / "otlp" / "otel-logs.json"
    ws_logs_root = Path.cwd() / "reflect" / "state" / "otel-logs.json"
    ws_logs = ws_logs_otlp if ws_logs_otlp.exists() else ws_logs_root

    home_logs = reflect_home / "state" / "otel-logs.json"
    if ws_logs.exists() and not home_logs.exists():
        home_logs.symlink_to(ws_logs)
        console.print(f"  [green]✓[/] Linked workspace logs → {ws_logs}")

    console.print("\n[bold]Step 5: Wire hook-based agents via opentelemetry-hooks[/]")
    if otel_hook:
        try:
            subprocess.check_call([otel_hook, "setup", "--global"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            console.print("  [green]✓[/] opentelemetry-hooks setup complete")
        except subprocess.CalledProcessError as exc:
            console.print(f"  [red]✗[/] opentelemetry-hooks setup failed (exit {exc.returncode})")
            console.print("    Run manually: [bold]otel-hook setup[/]")
    else:
        console.print("  [yellow]•[/] otel-hook not found; skipping hook-based agent wiring")
        console.print(f"    Install first: [bold]pipx install {_HOOK_PACKAGE_SPEC}[/]")

    console.print("\n[bold]Step 6: Enable native OTel (Claude Code, Copilot, Gemini, Codex)[/]")
    _configure_claude_native_otel(console, config)
    _configure_copilot_native_otel(console, config)
    _configure_copilot_cli_native_otel(console, config)
    _configure_gemini_native_otel(console, config)
    _configure_codex_native_otel(console, config)

    console.print("\n[bold]Step 6b: Start local OTLP gateway[/]")
    from reflect.gateway import _is_running as _gateway_is_running
    from reflect.gateway import daemon_start as _gateway_daemon_start

    if _gateway_is_running():
        console.print("  [green]✓[/] Gateway already running")
    else:
        try:
            pid = _gateway_daemon_start()
            console.print(f"  [green]✓[/] Gateway started (PID {pid}) — gRPC :4317 | HTTP :4318")
        except Exception as exc:
            console.print(f"  [red]✗[/] Failed to start gateway: {exc}")
            console.print("    Start manually: [bold]reflect gateway start[/]")

    planned_agents = [agent for agent in detected_agents if agent.get("support_status") != "Implemented"]
    if planned_agents:
        console.print("\n[bold yellow]Telemetry gaps still not implemented[/]")
        for agent in planned_agents:
            console.print(
                f"  [yellow]•[/] {agent['name']}: {agent['telemetry_path']}. "
                "reflect setup will not start collecting telemetry for this agent yet."
            )

    console.print("\n[bold]Step 7: Distribute AI Agent Skills[/]")
    distribute_skills(console)

    console.print("\n[bold]Step 8: Next steps[/]")
    console.print(f"[bold green]Done![/] Data will be written to [bold]{reflect_home}/state/[/]")
    console.print("\nRun [bold]reflect doctor[/] to confirm capture health, then run [bold]reflect[/] to view your dashboard.")
    console.print()
