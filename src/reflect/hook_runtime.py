from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOOK_COMMAND = "otel-hook.exe" if os.name == "nt" else "otel-hook"
HOOK_PACKAGE = "opentelemetry-hooks"
REFLECT_PACKAGE = "o11y-reflect"


class HookMigrationError(RuntimeError):
    """Raised when the legacy pipx hook environment cannot be migrated safely."""


@dataclass(frozen=True)
class HookRuntime:
    """A resolved hook executable, preferably from Reflect's own environment."""

    executable: Path
    bundled: bool

    @classmethod
    def discover(cls) -> HookRuntime | None:
        bundled_executable = Path(sys.executable).with_name(HOOK_COMMAND)
        if bundled_executable.is_file():
            return cls(bundled_executable, bundled=True)

        path_executable = shutil.which("otel-hook")
        if path_executable:
            return cls(Path(path_executable), bundled=False)
        return None


@dataclass(frozen=True)
class PipxPackage:
    name: str
    version: str | None
    package_or_url: str | None
    app_paths: tuple[Path, ...]

    @property
    def rollback_spec(self) -> str:
        if self.package_or_url:
            source_path = Path(self.package_or_url).expanduser()
            if source_path.exists():
                return str(source_path)
        if self.version:
            return f"{self.name}=={self.version}"
        return self.name


@dataclass(frozen=True)
class HookMigrationResult:
    action: str
    bundled_executable: Path
    standalone_version: str | None = None


class HookPipxMigrator:
    """Move the public hook command from a legacy pipx venv into Reflect's venv."""

    def __init__(
        self,
        pipx: str,
        *,
        run: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.pipx = pipx
        self._run = run or subprocess.run
        self._which = which or shutil.which

    def migrate(self) -> HookMigrationResult:
        inventory = self._load_inventory()
        reflect_package = self._package(inventory, REFLECT_PACKAGE)
        if reflect_package is None:
            raise HookMigrationError("pipx does not report an o11y-reflect environment")

        bundled_executable = self._hook_app_path(reflect_package)
        if bundled_executable is None or not bundled_executable.is_file():
            raise HookMigrationError(
                "the upgraded Reflect environment does not contain its bundled otel-hook command"
            )
        self._validate_cli(bundled_executable)

        standalone = self._package(inventory, HOOK_PACKAGE)
        if standalone is None:
            public_executable = self._public_hook()
            if public_executable is None:
                self._run_checked([self.pipx, "upgrade", "--force", REFLECT_PACKAGE])
                public_executable = self._public_hook()
                action = "repaired"
            else:
                action = "already-bundled"
            self._validate_public_command(public_executable, bundled_executable)
            return HookMigrationResult(action, bundled_executable)

        try:
            self._run_checked([self.pipx, "upgrade", "--force", REFLECT_PACKAGE])
            public_executable = self._public_hook()
            self._validate_public_command(public_executable, bundled_executable)
        except Exception as handoff_error:
            raise HookMigrationError(
                f"could not hand the public otel-hook command to Reflect; {HOOK_PACKAGE} was not removed"
            ) from handoff_error

        try:
            self._run_checked([self.pipx, "uninstall", HOOK_PACKAGE])
            public_executable = self._public_hook()
            self._validate_public_command(public_executable, bundled_executable)
        except Exception as migration_error:
            rollback_error = self._rollback(standalone)
            detail = (
                f"; rollback failed: {rollback_error}"
                if rollback_error
                else "; rollback restored it"
            )
            raise HookMigrationError(
                f"standalone {HOOK_PACKAGE} removal could not be completed{detail}"
            ) from migration_error

        return HookMigrationResult(
            "migrated",
            bundled_executable,
            standalone_version=standalone.version,
        )

    def _load_inventory(self) -> Mapping[str, Any]:
        completed = self._run_command([self.pipx, "list", "--json"])
        if completed.returncode:
            raise HookMigrationError(
                f"could not inspect pipx environments: {completed.stderr.strip() or 'pipx list failed'}"
            )
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HookMigrationError("pipx returned an invalid JSON inventory") from exc
        if not isinstance(payload, dict):
            raise HookMigrationError("pipx returned an invalid environment inventory")
        return payload

    @staticmethod
    def _package(inventory: Mapping[str, Any], package_name: str) -> PipxPackage | None:
        venvs = inventory.get("venvs")
        if not isinstance(venvs, dict):
            return None

        for venv_name, raw_entry in venvs.items():
            if not isinstance(raw_entry, dict):
                continue
            metadata = raw_entry.get("metadata", raw_entry)
            if not isinstance(metadata, dict):
                continue
            main_package = metadata.get("main_package")
            if not isinstance(main_package, dict):
                continue
            reported_name = str(main_package.get("package") or venv_name)
            if reported_name != package_name and str(venv_name) != package_name:
                continue
            app_paths = tuple(
                path
                for value in main_package.get("app_paths", [])
                if (path := HookPipxMigrator._decode_path(value)) is not None
            )
            version = main_package.get("package_version")
            package_or_url = main_package.get("package_or_url")
            return PipxPackage(
                name=package_name,
                version=str(version) if version else None,
                package_or_url=str(package_or_url) if package_or_url else None,
                app_paths=app_paths,
            )
        return None

    @staticmethod
    def _decode_path(value: object) -> Path | None:
        if isinstance(value, str):
            return Path(value)
        if isinstance(value, dict) and isinstance(value.get("__Path__"), str):
            return Path(value["__Path__"])
        return None

    @staticmethod
    def _hook_app_path(reflect_package: PipxPackage) -> Path | None:
        for app_path in reflect_package.app_paths:
            if app_path.name == HOOK_COMMAND:
                return app_path
        for app_path in reflect_package.app_paths:
            if app_path.name in {"reflect", "reflect.exe"}:
                return app_path.with_name(HOOK_COMMAND)
        return None

    def _public_hook(self) -> Path | None:
        executable = self._which("otel-hook")
        return Path(executable) if executable else None

    def _validate_cli(self, executable: Path) -> None:
        completed = self._run_command([str(executable), "--help"])
        if completed.returncode:
            raise HookMigrationError(
                f"bundled otel-hook failed its CLI check: {completed.stderr.strip()}"
            )

    def _validate_public_command(
        self,
        public_executable: Path | None,
        bundled_executable: Path,
    ) -> None:
        if public_executable is None:
            raise HookMigrationError("otel-hook is not exposed on PATH after the pipx handoff")
        if public_executable.resolve() != bundled_executable.resolve():
            raise HookMigrationError(
                f"otel-hook still resolves outside Reflect's environment: {public_executable}"
            )

        completed = self._run_command([str(public_executable), "doctor", "--json"])
        try:
            report = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError) as exc:
            raise HookMigrationError("otel-hook doctor did not return valid JSON") from exc
        if not isinstance(report, dict) or report.get("status") == "error":
            raise HookMigrationError("otel-hook doctor could not inspect the migrated runtime")

    def _rollback(self, standalone: PipxPackage) -> Exception | None:
        try:
            self._run_checked(
                [self.pipx, "install", "--force", standalone.rollback_spec]
            )
        except Exception as exc:  # rollback must preserve the original migration error
            return exc
        return None

    def _run_checked(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        completed = self._run_command(args)
        if completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode,
                args,
                output=completed.stdout,
                stderr=completed.stderr,
            )
        return completed

    def _run_command(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        return self._run(args, capture_output=True, text=True)
