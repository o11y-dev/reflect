"""Behavioral coverage for the public curl installer."""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "docs" / "install.sh"


def _write_executable(path: Path, contents: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(contents).lstrip(), encoding="utf-8")
    path.chmod(0o755)
    return path


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path]:
    home = tmp_path / "home"
    fake_bin = tmp_path / "bin"
    public_bin = home / ".local" / "bin"
    log = tmp_path / "install.log"
    fake_bin.mkdir(parents=True)
    public_bin.mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": os.pathsep.join((str(fake_bin), str(public_bin), "/usr/bin", "/bin")),
        "INSTALL_LOG": str(log),
    }
    return env, fake_bin, public_bin, log


def _reflect_fixture(tmp_path: Path) -> Path:
    return _write_executable(
        tmp_path / "reflect-fixture",
        """
        #!/bin/sh
        printf 'reflect %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/sh", str(INSTALLER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_installer_prefers_pipx_and_runs_setup_after_fresh_install(tmp_path: Path):
    env, fake_bin, _, log = _environment(tmp_path)
    fixture = _reflect_fixture(tmp_path)
    venvs = Path(env["HOME"]) / ".local" / "pipx" / "venvs"
    reflect_bin = venvs / "o11y-reflect" / "bin" / "reflect"
    reflect_bin.parent.mkdir(parents=True)
    reflect_bin.write_bytes(fixture.read_bytes())
    reflect_bin.chmod(0o755)
    env["PIPX_VENVS"] = str(venvs)
    _write_executable(
        fake_bin / "pipx",
        """
        #!/bin/sh
        printf 'pipx %s\n' "$*" >> "$INSTALL_LOG"
        [ "$1" = "runpip" ] && exit 1
        [ "$1" = "environment" ] && printf '%s\n' "$PIPX_VENVS"
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "uv",
        """
        #!/bin/sh
        printf 'uv %s\n' "$*" >> "$INSTALL_LOG"
        exit 0
        """,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "pipx install o11y-reflect" in calls
    assert "uv tool install" not in calls
    assert "reflect setup --all-agents --text-capture-mode metadata" in calls


def test_installer_uses_uv_when_pipx_is_unavailable(tmp_path: Path):
    env, fake_bin, _, log = _environment(tmp_path)
    fixture = _reflect_fixture(tmp_path)
    tools = Path(env["HOME"]) / ".local" / "share" / "uv" / "tools"
    env.update({"UV_TOOLS": str(tools), "REFLECT_FIXTURE": str(fixture)})
    _write_executable(
        fake_bin / "uv",
        """
        #!/bin/sh
        printf 'uv %s\n' "$*" >> "$INSTALL_LOG"
        if [ "$1 $2" = "tool dir" ]; then
            printf '%s\n' "$UV_TOOLS"
        elif [ "$1 $2" = "tool install" ]; then
            mkdir -p "$UV_TOOLS/o11y-reflect/bin"
            cp "$REFLECT_FIXTURE" "$UV_TOOLS/o11y-reflect/bin/reflect"
            chmod 755 "$UV_TOOLS/o11y-reflect/bin/reflect"
        fi
        """,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "uv tool install o11y-reflect" in calls
    assert "reflect setup --all-agents --text-capture-mode metadata" in calls


def test_installer_uses_an_isolated_environment_for_pip_fallback(tmp_path: Path):
    env, fake_bin, public_bin, log = _environment(tmp_path)
    fixture = _reflect_fixture(tmp_path)
    venv_python_fixture = _write_executable(
        tmp_path / "venv-python-fixture",
        """
        #!/bin/sh
        printf 'venv-python %s\n' "$*" >> "$INSTALL_LOG"
        if [ "$1 $2" = "-m pip" ]; then
            target=$(dirname "$0")
            for name in reflect reflect-mcp otel-hook; do
                cp "$REFLECT_FIXTURE" "$target/$name"
                chmod 755 "$target/$name"
            done
        fi
        """,
    )
    env.update(
        {
            "REFLECT_FIXTURE": str(fixture),
            "VENV_PYTHON_FIXTURE": str(venv_python_fixture),
        }
    )
    _write_executable(
        fake_bin / "python3",
        """
        #!/bin/sh
        printf 'python3 %s\n' "$*" >> "$INSTALL_LOG"
        [ "$1 $2 $3" = "-m pip --version" ] && exit 0
        [ "$1" = "-c" ] && exit 0
        if [ "$1 $2" = "-m venv" ]; then
            mkdir -p "$3/bin"
            cp "$VENV_PYTHON_FIXTURE" "$3/bin/python"
            chmod 755 "$3/bin/python"
            exit 0
        fi
        exit 1
        """,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "python3 -m venv" in calls
    assert "venv-python -m pip install --upgrade o11y-reflect" in calls
    assert "reflect setup --all-agents --text-capture-mode metadata" in calls
    assert (public_bin / "reflect").is_symlink()


def test_installer_updates_the_active_uv_owner_without_rerunning_setup(tmp_path: Path):
    env, fake_bin, public_bin, log = _environment(tmp_path)
    tools = Path(env["HOME"]) / ".local" / "share" / "uv" / "tools"
    reflect_bin = _write_executable(
        tools / "o11y-reflect" / "bin" / "reflect",
        """
        #!/bin/sh
        printf 'reflect %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )
    (public_bin / "reflect").symlink_to(reflect_bin)
    _write_executable(
        fake_bin / "pipx",
        """
        #!/bin/sh
        printf 'pipx %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )
    _write_executable(
        fake_bin / "uv",
        """
        #!/bin/sh
        printf 'uv %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "uv tool upgrade o11y-reflect" in calls
    assert "pipx " not in calls
    assert "reflect setup" not in calls


def test_installer_uses_reflects_pipx_updater_when_available(tmp_path: Path):
    env, fake_bin, public_bin, log = _environment(tmp_path)
    venvs = Path(env["HOME"]) / ".local" / "pipx" / "venvs"
    reflect_bin = _write_executable(
        venvs / "o11y-reflect" / "bin" / "reflect",
        """
        #!/bin/sh
        printf 'reflect %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )
    (public_bin / "reflect").symlink_to(reflect_bin)
    _write_executable(
        fake_bin / "pipx",
        """
        #!/bin/sh
        printf 'pipx %s\n' "$*" >> "$INSTALL_LOG"
        """,
    )

    result = _run(env)

    assert result.returncode == 0, result.stderr
    calls = log.read_text(encoding="utf-8")
    assert "reflect update --help" in calls
    assert "reflect update --apply" in calls
    assert "reflect setup" not in calls


def test_installer_is_executable_and_avoids_privileged_pip_mutations():
    text = INSTALLER.read_text(encoding="utf-8")
    mode = INSTALLER.stat().st_mode

    assert text.startswith("#!/bin/sh\nset -eu\n")
    assert "pipx install" in text
    assert "uv tool install" in text
    assert "-m venv" in text
    assert "sudo" not in text
    assert "--break-system-packages" not in text
    assert mode & stat.S_IXUSR
