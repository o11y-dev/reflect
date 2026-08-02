from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reflect.autostart import LaunchdAutostartManager, create_autostart_manager
from reflect.core import main


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded: set[str] = set()
        self.commands: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        operation = command[1]
        if operation == "print":
            label = command[2].rsplit("/", 1)[-1]
            return self._result(command, 0 if label in self.loaded else 113)
        if operation == "bootstrap":
            definition = plistlib.loads(Path(command[3]).read_bytes())
            self.loaded.add(definition["Label"])
            return self._result(command, 0)
        if operation == "bootout":
            self.loaded.discard(command[2].rsplit("/", 1)[-1])
            return self._result(command, 0)
        return self._result(command, 1, stderr="unexpected launchctl operation")

    @staticmethod
    def _result(
        command: list[str],
        returncode: int,
        *,
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout="",
            stderr=stderr,
        )


def _manager(tmp_path: Path, launchctl: FakeLaunchctl) -> LaunchdAutostartManager:
    return LaunchdAutostartManager(
        reflect_home=tmp_path / ".reflect",
        launch_agents_dir=tmp_path / "Library" / "LaunchAgents",
        python_executable="/test/reflect/bin/python",
        launchctl="/bin/launchctl",
        uid=501,
        command_runner=launchctl,
    )


def test_launchd_enable_installs_and_loads_both_services(tmp_path):
    launchctl = FakeLaunchctl()
    manager = _manager(tmp_path, launchctl)

    statuses = manager.enable()

    assert [status.name for status in statuses] == ["OTLP gateway", "report server"]
    assert all(status.enabled and status.loaded for status in statuses)
    definitions = {
        status.label: plistlib.loads(status.definition_path.read_bytes())
        for status in statuses
    }
    gateway = definitions["dev.o11y.reflect.gateway"]
    report = definitions["dev.o11y.reflect.report-server"]
    assert gateway["RunAtLoad"] is True
    assert gateway["ProgramArguments"][:3] == [
        "/test/reflect/bin/python",
        "-m",
        "reflect.gateway",
    ]
    assert report["ProgramArguments"][-2:] == ["--refresh", "--no-open-browser"]
    assert report["EnvironmentVariables"]["REFLECT_HOME"] == str(
        (tmp_path / ".reflect").resolve()
    )


def test_launchd_enable_is_idempotent_and_disable_removes_definitions(tmp_path):
    launchctl = FakeLaunchctl()
    manager = _manager(tmp_path, launchctl)

    first = manager.enable()
    bootstrap_count = sum(command[1] == "bootstrap" for command in launchctl.commands)
    second = manager.enable()

    assert bootstrap_count == 2
    assert sum(command[1] == "bootstrap" for command in launchctl.commands) == 2
    assert first == second

    disabled = manager.disable()

    assert all(not status.enabled and not status.loaded for status in disabled)
    assert not any(status.definition_path.exists() for status in first)


def test_create_autostart_manager_is_explicitly_platform_scoped(tmp_path):
    assert create_autostart_manager(tmp_path, system="Linux") is None


def test_autostart_cli_renders_service_state(tmp_path):
    statuses = (
        SimpleNamespace(
            name="OTLP gateway",
            enabled=True,
            loaded=True,
            definition_path=tmp_path / "gateway.plist",
        ),
        SimpleNamespace(
            name="report server",
            enabled=True,
            loaded=True,
            definition_path=tmp_path / "report.plist",
        ),
    )
    manager = SimpleNamespace(
        platform_name="macOS launchd",
        enable=MagicMock(return_value=statuses),
        status=MagicMock(return_value=statuses),
    )
    runner = CliRunner()

    with patch("reflect.core._get_autostart_manager", return_value=manager):
        result = runner.invoke(main, ["autostart", "enable"])

    assert result.exit_code == 0
    assert "auto-start enabled" in result.output
    assert "OTLP gateway" in result.output
    assert "report server" in result.output
    manager.enable.assert_called_once_with()
