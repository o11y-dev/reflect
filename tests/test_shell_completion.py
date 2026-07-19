"""Tests for Click-native shell completion generation and local ID suggestions."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import click
import pytest
from click.shell_completion import CompletionItem
from click.testing import CliRunner

import reflect.core as core
from reflect.core import main
from reflect.shell_completion import (
    SUPPORTED_SHELLS,
    ShellCompletionManager,
    SqliteCompletionCatalog,
)


@pytest.mark.parametrize("shell", SUPPORTED_SHELLS)
def test_completion_command_generates_click_source(shell: str) -> None:
    result = CliRunner().invoke(main, ["completion", "--shell", shell])

    assert result.exit_code == 0
    assert "reflect" in result.output
    assert "_REFLECT_COMPLETE" in result.output


def test_completion_manager_installs_zsh_idempotently(tmp_path: Path) -> None:
    manager = ShellCompletionManager(main, home=tmp_path)

    first = manager.install("zsh")
    second = manager.install("zsh")

    assert first.changed is True
    assert second.changed is False
    assert first.script_path.is_file()
    zshrc = (tmp_path / ".zshrc").read_text(encoding="utf-8")
    assert zshrc.count(">>> reflect shell completion >>>") == 1
    assert str(first.script_path) in zshrc


def test_completion_manager_uses_fish_autoload_directory(tmp_path: Path) -> None:
    result = ShellCompletionManager(main, home=tmp_path).install("fish")

    assert result.script_path == tmp_path / ".config" / "fish" / "completions" / "reflect.fish"
    assert result.config_path is None
    assert result.script_path.is_file()


def test_setup_can_explicitly_install_shell_completion() -> None:
    install_result = SimpleNamespace(changed=True, script_path=Path("/tmp/reflect.zsh"))
    with (
        patch.object(core, "_run_setup"),
        patch.object(core, "_detect_agents", return_value=[]),
        patch.object(core.ShellCompletionManager, "detect_shell", return_value="zsh"),
        patch.object(core.ShellCompletionManager, "install", return_value=install_result) as install,
    ):
        result = CliRunner().invoke(
            main,
            ["setup", "--text-capture-mode", "metadata", "--shell-completion"],
        )

    assert result.exit_code == 0
    install.assert_called_once_with("zsh")
    assert "Shell autocomplete installed" in result.output


def test_setup_warns_when_optional_shell_completion_cannot_be_written() -> None:
    with (
        patch.object(core, "_run_setup") as run_setup,
        patch.object(core, "_detect_agents", return_value=[]),
        patch.object(core.ShellCompletionManager, "detect_shell", return_value="zsh"),
        patch.object(
            core.ShellCompletionManager,
            "install",
            side_effect=PermissionError(".zshrc is read-only"),
        ),
    ):
        result = CliRunner().invoke(
            main,
            ["setup", "--text-capture-mode", "metadata", "--shell-completion"],
        )

    assert result.exit_code == 0
    run_setup.assert_called_once()
    assert "Telemetry setup completed" in result.output
    assert "shell autocomplete could not be installed" in result.output


def test_click_completion_covers_root_and_nested_commands() -> None:
    runner = CliRunner()
    root = runner.invoke(
        main,
        [],
        prog_name="reflect",
        env={
            "_REFLECT_COMPLETE": "bash_complete",
            "COMP_WORDS": "reflect wor",
            "COMP_CWORD": "1",
        },
    )
    nested = runner.invoke(
        main,
        [],
        prog_name="reflect",
        env={
            "_REFLECT_COMPLETE": "bash_complete",
            "COMP_WORDS": "reflect workflows sh",
            "COMP_CWORD": "2",
        },
    )

    assert root.exit_code == 0
    assert "workflows" in root.output
    assert nested.exit_code == 0
    assert "show" in nested.output


def test_click_completion_suggests_setup_agent_aliases() -> None:
    result = CliRunner().invoke(
        main,
        [],
        prog_name="reflect",
        env={
            "_REFLECT_COMPLETE": "bash_complete",
            "COMP_WORDS": "reflect setup --agent co",
            "COMP_CWORD": "3",
        },
    )

    assert result.exit_code == 0
    assert "codex" in result.output
    assert "copilot" in result.output


def test_sqlite_catalog_returns_bounded_prefix_matches_without_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "reflect.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE skills (id TEXT PRIMARY KEY, slug TEXT, updated_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO skills(id, slug, updated_at) VALUES (?, ?, ?)",
            [
                ("skill_alpha", "alpha", "2026-01-01"),
                ("skill_beta", "beta", "2026-01-02"),
            ],
        )

    items = SqliteCompletionCatalog(limit=10).complete("skill", "skill_a", db_path)

    assert all(isinstance(item, CompletionItem) for item in items)
    assert [(item.value, item.help) for item in items] == [("skill_alpha", "alpha")]


def test_sqlite_catalog_does_not_create_a_missing_database(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    items = SqliteCompletionCatalog().complete("workflow", "wf_", db_path)

    assert items == []
    assert not db_path.exists()


@click.command()
def sample_cli() -> None:
    pass


def test_completion_manager_rejects_unsupported_shell(tmp_path: Path) -> None:
    manager = ShellCompletionManager(sample_cli, home=tmp_path)

    with pytest.raises(ValueError, match="Unsupported shell"):
        manager.source("powershell")
