"""Rich CLI progress rendering for snapshot preparation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import nullcontext
from typing import TypeVar

from reflect.preparation import PreparationProgress, PreparationProgressReporter

_PreparationResult = TypeVar("_PreparationResult")


class TerminalPreparationProgress:
    """Render typed preparation stages without contaminating stdout."""

    def __init__(self, console=None) -> None:
        if console is None:
            from rich.console import Console

            console = Console(stderr=True)
        self.console = console

    def run(
        self,
        prepare: Callable[[PreparationProgressReporter], _PreparationResult],
        *,
        initial_message: str = "Opening the local telemetry store...",
    ) -> _PreparationResult:
        status_context = (
            self.console.status(
                f"[bold orange3]{initial_message}[/bold orange3]",
                spinner="dots",
            )
            if self.console.is_terminal
            else nullcontext()
        )
        with status_context as status:

            def update(progress: PreparationProgress) -> None:
                message = f"[bold orange3]{progress.message}[/bold orange3]"
                if status is None:
                    self.console.print(message)
                else:
                    status.update(message)

            return prepare(update)


__all__ = ["TerminalPreparationProgress"]
