from __future__ import annotations

import io

from rich.console import Console

from reflect.cli.progress import TerminalPreparationProgress
from reflect.preparation import PreparationProgress, PreparationStage


def test_terminal_preparation_progress_keeps_non_terminal_feedback_visible():
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, highlight=False)

    result = TerminalPreparationProgress(console).run(
        lambda report: (
            report(
                PreparationProgress(
                    stage=PreparationStage.INGESTING_SESSIONS,
                    message="Reading local agent sessions...",
                )
            )
            or {"sessions": 2}
        )
    )

    assert result == {"sessions": 2}
    assert "Reading local agent sessions..." in output.getvalue()
