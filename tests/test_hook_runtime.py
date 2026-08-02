import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from reflect.hook_runtime import (
    HOOK_COMMAND,
    HookMigrationError,
    HookPipxMigrator,
    HookRuntime,
)


def _completed(args: list[str], *, stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _inventory(bundled_hook: Path, *, standalone: bool = True) -> str:
    venvs = {
        "o11y-reflect": {
            "metadata": {
                "main_package": {
                    "package": "o11y-reflect",
                    "package_version": "0.9.5",
                    "package_or_url": "o11y-reflect",
                    "app_paths": [
                        {"__Path__": str(bundled_hook.with_name("reflect"))},
                        {"__Path__": str(bundled_hook)},
                    ],
                }
            }
        }
    }
    if standalone:
        venvs["opentelemetry-hooks"] = {
            "metadata": {
                "main_package": {
                    "package": "opentelemetry-hooks",
                    "package_version": "0.14.0",
                    "package_or_url": "opentelemetry-hooks",
                    "app_paths": [{"__Path__": "/legacy/bin/otel-hook"}],
                }
            }
        }
    return json.dumps({"venvs": venvs})


def test_project_bundles_hooks_and_exposes_existing_command():
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]

    assert project["requires-python"] == ">=3.12"
    assert "opentelemetry-hooks>=0.14,<0.15" in project["dependencies"]
    assert project["scripts"]["otel-hook"] == "otel_hook:cli"


def test_hook_runtime_prefers_current_environment(monkeypatch, tmp_path):
    python = tmp_path / "bin" / "python"
    python.parent.mkdir()
    python.touch()
    bundled_hook = python.with_name(HOOK_COMMAND)
    bundled_hook.touch()
    monkeypatch.setattr("reflect.hook_runtime.sys.executable", str(python))
    monkeypatch.setattr(
        "reflect.hook_runtime.shutil.which",
        lambda _name: "/legacy/bin/otel-hook",
    )

    runtime = HookRuntime.discover()

    assert runtime == HookRuntime(bundled_hook, bundled=True)


def test_pipx_migration_uninstalls_standalone_after_validation(tmp_path):
    bundled_hook = tmp_path / "reflect-venv" / "bin" / HOOK_COMMAND
    bundled_hook.parent.mkdir(parents=True)
    bundled_hook.touch()
    calls: list[list[str]] = []
    public = {"path": "/legacy/bin/otel-hook"}

    def run(args, **_kwargs):
        calls.append(args)
        if args == ["pipx", "list", "--json"]:
            return _completed(args, stdout=_inventory(bundled_hook))
        if args == [str(bundled_hook), "--help"]:
            return _completed(args, stdout="Usage: otel-hook")
        if args == ["pipx", "upgrade", "--force", "o11y-reflect"]:
            public["path"] = str(bundled_hook)
            return _completed(args)
        if args == ["pipx", "uninstall", "opentelemetry-hooks"]:
            return _completed(args)
        if args == [str(bundled_hook), "doctor", "--json"]:
            return _completed(args, stdout='{"status":"degraded"}')
        raise AssertionError(args)

    result = HookPipxMigrator(
        "pipx",
        run=run,
        which=lambda _name: public["path"],
    ).migrate()

    assert result.action == "migrated"
    assert result.standalone_version == "0.14.0"
    assert calls.index([str(bundled_hook), "--help"]) < calls.index(
        ["pipx", "upgrade", "--force", "o11y-reflect"]
    )
    assert calls.index(["pipx", "upgrade", "--force", "o11y-reflect"]) < calls.index(
        ["pipx", "uninstall", "opentelemetry-hooks"]
    )
    assert ["pipx", "install", "--force", "opentelemetry-hooks==0.14.0"] not in calls


def test_pipx_migration_validates_existing_bundled_command(tmp_path):
    bundled_hook = tmp_path / "reflect-venv" / "bin" / HOOK_COMMAND
    bundled_hook.parent.mkdir(parents=True)
    bundled_hook.touch()
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(args)
        if args == ["pipx", "list", "--json"]:
            return _completed(args, stdout=_inventory(bundled_hook, standalone=False))
        if args == [str(bundled_hook), "--help"]:
            return _completed(args)
        if args == [str(bundled_hook), "doctor", "--json"]:
            return _completed(args, stdout='{"status":"healthy"}')
        raise AssertionError(args)

    result = HookPipxMigrator(
        "pipx",
        run=run,
        which=lambda _name: str(bundled_hook),
    ).migrate()

    assert result.action == "already-bundled"
    assert ["pipx", "uninstall", "opentelemetry-hooks"] not in calls


def test_pipx_migration_preserves_standalone_when_public_handoff_fails(tmp_path):
    bundled_hook = tmp_path / "reflect-venv" / "bin" / HOOK_COMMAND
    bundled_hook.parent.mkdir(parents=True)
    bundled_hook.touch()
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(args)
        if args == ["pipx", "list", "--json"]:
            return _completed(args, stdout=_inventory(bundled_hook))
        if args == [str(bundled_hook), "--help"]:
            return _completed(args)
        if args == ["pipx", "upgrade", "--force", "o11y-reflect"]:
            return _completed(args, stderr="expose failed", returncode=1)
        raise AssertionError(args)

    with pytest.raises(HookMigrationError, match="was not removed"):
        HookPipxMigrator(
            "pipx",
            run=run,
            which=lambda _name: "/legacy/bin/otel-hook",
        ).migrate()

    assert ["pipx", "uninstall", "opentelemetry-hooks"] not in calls


def test_pipx_migration_rolls_back_when_post_uninstall_validation_fails(tmp_path):
    bundled_hook = tmp_path / "reflect-venv" / "bin" / HOOK_COMMAND
    bundled_hook.parent.mkdir(parents=True)
    bundled_hook.touch()
    calls: list[list[str]] = []
    doctor_calls = 0
    public = {"path": "/legacy/bin/otel-hook"}

    def run(args, **_kwargs):
        nonlocal doctor_calls
        calls.append(args)
        if args == ["pipx", "list", "--json"]:
            return _completed(args, stdout=_inventory(bundled_hook))
        if args == [str(bundled_hook), "--help"]:
            return _completed(args)
        if args == ["pipx", "upgrade", "--force", "o11y-reflect"]:
            public["path"] = str(bundled_hook)
            return _completed(args)
        if args == ["pipx", "uninstall", "opentelemetry-hooks"]:
            return _completed(args)
        if args == [str(bundled_hook), "doctor", "--json"]:
            doctor_calls += 1
            status = "healthy" if doctor_calls == 1 else "error"
            return _completed(args, stdout=json.dumps({"status": status}))
        if args == ["pipx", "install", "--force", "opentelemetry-hooks==0.14.0"]:
            return _completed(args)
        raise AssertionError(args)

    with pytest.raises(HookMigrationError, match="rollback restored it"):
        HookPipxMigrator(
            "pipx",
            run=run,
            which=lambda _name: public["path"],
        ).migrate()

    assert calls[-1] == [
        "pipx",
        "install",
        "--force",
        "opentelemetry-hooks==0.14.0",
    ]


def test_pipx_migration_does_not_uninstall_when_bundled_cli_is_missing(tmp_path):
    missing_hook = tmp_path / "reflect-venv" / "bin" / HOOK_COMMAND
    calls: list[list[str]] = []

    def run(args, **_kwargs):
        calls.append(args)
        return _completed(args, stdout=_inventory(missing_hook))

    with pytest.raises(HookMigrationError, match="does not contain"):
        HookPipxMigrator("pipx", run=run).migrate()

    assert ["pipx", "uninstall", "opentelemetry-hooks"] not in calls
