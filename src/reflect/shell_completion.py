"""Shell-completion support for the Reflect Click command tree."""

from __future__ import annotations

import os
import re
import shlex
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import quote

import click
from click.shell_completion import CompletionItem, get_completion_class

if TYPE_CHECKING:
    from collections.abc import Callable


SUPPORTED_SHELLS = ("bash", "zsh", "fish")
MEMORY_PROVIDER_NAMES = (
    "local_sqlite",
    "omega",
    "agentmemory",
    "litellm",
    "memorypalace",
    "mem0",
    "graphiti",
    "tencentdb_agent_memory",
)
_COMPLETE_VAR = "_REFLECT_COMPLETE"
_MANAGED_START = "# >>> reflect shell completion >>>"
_MANAGED_END = "# <<< reflect shell completion <<<"


@dataclass(frozen=True)
class CompletionInstallResult:
    """One idempotent shell-completion installation result."""

    shell: str
    script_path: Path
    config_path: Path | None
    changed: bool


class ShellCompletionManager:
    """Generate and install Click completion scripts for one CLI."""

    def __init__(
        self,
        cli: click.Command,
        *,
        prog_name: str = "reflect",
        complete_var: str = _COMPLETE_VAR,
        home: Path | None = None,
    ) -> None:
        self.cli = cli
        self.prog_name = prog_name
        self.complete_var = complete_var
        self.home = home or Path.home()

    def detect_shell(self, shell_env: str | None = None) -> str | None:
        """Return a supported shell name from ``$SHELL`` or an explicit path."""
        shell_name = Path(shell_env or os.environ.get("SHELL", "")).name.lower()
        return shell_name if shell_name in SUPPORTED_SHELLS else None

    def source(self, shell: str) -> str:
        """Render Click's activation script for ``shell``."""
        completion_class = get_completion_class(shell)
        if completion_class is None or shell not in SUPPORTED_SHELLS:
            supported = ", ".join(SUPPORTED_SHELLS)
            raise ValueError(f"Unsupported shell {shell!r}; choose one of: {supported}")
        return completion_class(
            self.cli,
            {},
            self.prog_name,
            self.complete_var,
        ).source()

    def install(self, shell: str) -> CompletionInstallResult:
        """Install a generated script and activate it in the shell idempotently."""
        script_path, config_path = self._installation_paths(shell)
        script = self.source(shell)
        script_changed = self._write_if_changed(script_path, script)

        config_changed = False
        if config_path is not None:
            source_line = f"[ -f {shlex.quote(str(script_path))} ] && . {shlex.quote(str(script_path))}"
            config_changed = self._upsert_managed_block(config_path, source_line)

        return CompletionInstallResult(
            shell=shell,
            script_path=script_path,
            config_path=config_path,
            changed=script_changed or config_changed,
        )

    def _installation_paths(self, shell: str) -> tuple[Path, Path | None]:
        if shell == "fish":
            return self.home / ".config" / "fish" / "completions" / "reflect.fish", None
        if shell == "bash":
            return self.home / ".local" / "share" / "reflect" / "completions" / "reflect.bash", self.home / ".bashrc"
        if shell == "zsh":
            return self.home / ".local" / "share" / "reflect" / "completions" / "reflect.zsh", self.home / ".zshrc"
        supported = ", ".join(SUPPORTED_SHELLS)
        raise ValueError(f"Unsupported shell {shell!r}; choose one of: {supported}")

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        normalized = content if content.endswith("\n") else content + "\n"
        if path.exists() and path.read_text(encoding="utf-8") == normalized:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(normalized, encoding="utf-8")
        return True

    @staticmethod
    def _upsert_managed_block(path: Path, source_line: str) -> bool:
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        block = f"{_MANAGED_START}\n{source_line}\n{_MANAGED_END}"
        pattern = re.compile(
            rf"{re.escape(_MANAGED_START)}.*?{re.escape(_MANAGED_END)}",
            flags=re.DOTALL,
        )
        if pattern.search(existing):
            updated = pattern.sub(lambda _match: block, existing)
        else:
            separator = "" if not existing else ("" if existing.endswith("\n\n") else "\n")
            updated = f"{existing}{separator}{block}\n"
        if updated == existing:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
        return True


@dataclass(frozen=True)
class CompletionQuery:
    """A bounded, read-only completion query over the local Reflect store."""

    table: str
    value: str
    label: str
    order_by: str
    distinct: bool = False


class SqliteCompletionCatalog:
    """Return privacy-safe completion labels without creating or migrating a DB."""

    _queries = {
        "observation": CompletionQuery("observations", "id", "title", "updated_at DESC"),
        "workflow": CompletionQuery("workflow_candidates", "id", "title", "updated_at DESC"),
        "loop": CompletionQuery("loop_patterns", "id", "title", "updated_at DESC"),
        "session": CompletionQuery(
            "sessions",
            "id",
            "status || ' · ' || started_at",
            "started_at DESC",
        ),
        "skill": CompletionQuery("skills", "id", "slug", "updated_at DESC"),
        "memory": CompletionQuery("memories", "id", "type || ' · ' || scope", "updated_at DESC"),
        "memory_candidate": CompletionQuery(
            "memory_candidates",
            "id",
            "type || ' · ' || scope",
            "updated_at DESC",
        ),
        "memory_type": CompletionQuery("memories", "type", "type", "type", distinct=True),
        "memory_scope": CompletionQuery("memories", "scope", "scope", "scope", distinct=True),
        "memory_source": CompletionQuery("memories", "source", "source", "source", distinct=True),
        "memory_provider": CompletionQuery("memories", "provider", "provider", "provider", distinct=True),
    }

    def __init__(self, *, limit: int = 100) -> None:
        self.limit = limit

    def complete(self, entity: str, incomplete: str, db_path: Path) -> list[CompletionItem]:
        query = self._queries[entity]
        if not db_path.is_file():
            return []
        prefix = self._escape_like(incomplete) + "%"
        distinct = "DISTINCT " if query.distinct else ""
        sql = (
            f"SELECT {distinct}{query.value}, {query.label} "
            f"FROM {query.table} "
            f"WHERE {query.value} LIKE ? ESCAPE '\\' "
            f"ORDER BY {query.order_by} LIMIT ?"
        )
        uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True) as conn:
                rows = conn.execute(sql, (prefix, self.limit)).fetchall()
        except (OSError, sqlite3.Error):
            return []
        return [
            CompletionItem(str(value), help=str(label or ""))
            for value, label in rows
            if value is not None
        ]

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


_SQLITE_COMPLETIONS = SqliteCompletionCatalog()


def _db_path_from_context(ctx: click.Context) -> Path:
    current: click.Context | None = ctx
    while current is not None:
        db_path = current.params.get("db_path")
        if db_path:
            return Path(db_path)
        current = current.parent
    reflect_home = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
    return reflect_home / "state" / "reflect.db"


def sqlite_completer(entity: str) -> Callable[[click.Context, click.Parameter, str], list[CompletionItem]]:
    """Build a Click callback for one registered SQLite-backed entity."""

    def complete(ctx: click.Context, _param: click.Parameter, incomplete: str) -> list[CompletionItem]:
        return _SQLITE_COMPLETIONS.complete(entity, incomplete, _db_path_from_context(ctx))

    return complete


complete_observation_id = sqlite_completer("observation")
complete_workflow_id = sqlite_completer("workflow")
complete_loop_id = sqlite_completer("loop")
complete_session_id = sqlite_completer("session")
complete_skill_id = sqlite_completer("skill")
complete_memory_id = sqlite_completer("memory")
complete_memory_candidate_id = sqlite_completer("memory_candidate")
complete_memory_type = sqlite_completer("memory_type")
complete_memory_scope = sqlite_completer("memory_scope")
complete_memory_source = sqlite_completer("memory_source")


def complete_memory_provider(
    ctx: click.Context,
    _param: click.Parameter,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete built-in provider names plus providers observed in local memory rows."""
    observed = _SQLITE_COMPLETIONS.complete(
        "memory_provider",
        incomplete,
        _db_path_from_context(ctx),
    )
    items = {item.value: item for item in observed}
    for name in MEMORY_PROVIDER_NAMES:
        if name.startswith(incomplete):
            items.setdefault(name, CompletionItem(name, help="memory provider"))
    return [items[name] for name in sorted(items)]
