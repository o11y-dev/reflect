"""Persistent user-service startup for Reflect background processes."""

from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class AutostartService:
    """One long-running Reflect process managed by the operating system."""

    name: str
    label: str
    program_arguments: tuple[str, ...]
    log_file: Path


@dataclass(frozen=True)
class AutostartServiceStatus:
    """Installed and loaded state for one persistent user service."""

    name: str
    label: str
    enabled: bool
    loaded: bool
    definition_path: Path


class AutostartManager(Protocol):
    """Small platform adapter for persistent Reflect user services."""

    platform_name: str

    def enable(self) -> tuple[AutostartServiceStatus, ...]: ...

    def disable(self) -> tuple[AutostartServiceStatus, ...]: ...

    def status(self) -> tuple[AutostartServiceStatus, ...]: ...


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class LaunchdAutostartManager:
    """Install and manage macOS LaunchAgents for Reflect's local services."""

    platform_name = "macOS launchd"

    def __init__(
        self,
        *,
        reflect_home: Path,
        launch_agents_dir: Path | None = None,
        python_executable: str | None = None,
        launchctl: str = "/bin/launchctl",
        uid: int | None = None,
        command_runner: CommandRunner | None = None,
    ) -> None:
        self.reflect_home = reflect_home.expanduser().resolve()
        self.launch_agents_dir = (
            launch_agents_dir or Path.home() / "Library" / "LaunchAgents"
        )
        self.python_executable = python_executable or sys.executable
        self.launchctl = launchctl
        self.uid = os.getuid() if uid is None else uid
        self._command_runner = command_runner or self._run_command

    @property
    def services(self) -> tuple[AutostartService, ...]:
        state_dir = self.reflect_home / "state"
        return (
            AutostartService(
                name="OTLP gateway",
                label="dev.o11y.reflect.gateway",
                program_arguments=(
                    self.python_executable,
                    "-m",
                    "reflect.gateway",
                    "--grpc-port",
                    "4317",
                    "--http-port",
                    "4318",
                ),
                log_file=state_dir / "gateway.log",
            ),
            AutostartService(
                name="report server",
                label="dev.o11y.reflect.report-server",
                program_arguments=(
                    self.python_executable,
                    "-m",
                    "reflect.report_server",
                    "--port",
                    "8765",
                    "--db-path",
                    str(state_dir / "reflect.db"),
                    "--refresh",
                    "--no-open-browser",
                ),
                log_file=state_dir / "report-server.log",
            ),
        )

    def enable(self) -> tuple[AutostartServiceStatus, ...]:
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        (self.reflect_home / "state").mkdir(parents=True, exist_ok=True)
        for service in self.services:
            definition_path = self._definition_path(service)
            rendered = self._render_definition(service)
            changed = not definition_path.exists() or definition_path.read_bytes() != rendered
            if changed:
                self._write_definition(definition_path, rendered)
            loaded = self._is_loaded(service)
            if changed and loaded:
                self._run_launchctl(
                    "bootout",
                    self._service_target(service),
                    action=f"reload {service.name}",
                )
                loaded = False
            if not loaded:
                self._run_launchctl(
                    "bootstrap",
                    self._domain_target,
                    str(definition_path),
                    action=f"enable {service.name}",
                )
        return self.status()

    def disable(self) -> tuple[AutostartServiceStatus, ...]:
        for service in self.services:
            if self._is_loaded(service):
                self._run_launchctl(
                    "bootout",
                    self._service_target(service),
                    action=f"disable {service.name}",
                )
            self._definition_path(service).unlink(missing_ok=True)
        return self.status()

    def status(self) -> tuple[AutostartServiceStatus, ...]:
        return tuple(
            AutostartServiceStatus(
                name=service.name,
                label=service.label,
                enabled=self._definition_path(service).is_file(),
                loaded=self._is_loaded(service),
                definition_path=self._definition_path(service),
            )
            for service in self.services
        )

    @property
    def _domain_target(self) -> str:
        return f"gui/{self.uid}"

    def _service_target(self, service: AutostartService) -> str:
        return f"{self._domain_target}/{service.label}"

    def _definition_path(self, service: AutostartService) -> Path:
        return self.launch_agents_dir / f"{service.label}.plist"

    def _render_definition(self, service: AutostartService) -> bytes:
        return plistlib.dumps(
            {
                "EnvironmentVariables": {"REFLECT_HOME": str(self.reflect_home)},
                "Label": service.label,
                "ProcessType": "Background",
                "ProgramArguments": list(service.program_arguments),
                "RunAtLoad": True,
                "StandardErrorPath": str(service.log_file),
                "StandardOutPath": str(service.log_file),
            },
            sort_keys=True,
        )

    @staticmethod
    def _write_definition(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(content)
            temporary.chmod(0o600)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _is_loaded(self, service: AutostartService) -> bool:
        result = self._command_runner(
            [self.launchctl, "print", self._service_target(service)]
        )
        return result.returncode == 0

    def _run_launchctl(self, *arguments: str, action: str) -> None:
        result = self._command_runner([self.launchctl, *arguments])
        if result.returncode == 0:
            return
        detail = (result.stderr or result.stdout or "unknown launchctl error").strip()
        raise RuntimeError(f"Could not {action}: {detail}")

    @staticmethod
    def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, capture_output=True, text=True, check=False)


def create_autostart_manager(
    reflect_home: Path,
    *,
    system: str | None = None,
) -> AutostartManager | None:
    """Return the native user-service adapter when this platform supports one."""
    if (system or platform.system()) != "Darwin":
        return None
    launchctl = shutil.which("launchctl")
    if launchctl is None:
        return None
    return LaunchdAutostartManager(
        reflect_home=reflect_home,
        launchctl=launchctl,
    )
