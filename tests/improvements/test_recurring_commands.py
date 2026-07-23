from __future__ import annotations

import pytest

from reflect.improvements.recurring_commands import RecurringCommandRegistry


@pytest.mark.parametrize(
    ("agent", "tool", "preview", "family", "command"),
    [
        (
            "Cursor",
            "shell",
            '{"cmd":"echo AGENT_LOOP_WAKE_mrchase"}',
            "scheduled_loop",
            "/loop",
        ),
        (
            "Claude Code",
            "CronCreate",
            '{"prompt":"check the deploy","recurring":true,"schedule":"*/5 * * * *"}',
            "scheduled_loop",
            "CronCreate",
        ),
        (
            "Claude Code",
            "user_prompt",
            '{"prompt":"/goal all auth tests pass"}',
            "goal",
            "/goal",
        ),
        (
            "GitHub Copilot",
            "user_prompt",
            '{"prompt":"/every 30m check open PR comments"}',
            "scheduled_loop",
            "/every",
        ),
        (
            "OpenAI Codex CLI",
            "functions.create_goal",
            '{"objective":"Complete the migration until tests pass"}',
            "goal",
            "/goal",
        ),
    ],
)
def test_registry_classifies_documented_native_continuations(
    agent: str,
    tool: str,
    preview: str,
    family: str,
    command: str,
) -> None:
    match = RecurringCommandRegistry().classify(
        agent_name=agent,
        tool_name=tool,
        preview=preview,
    )

    assert match is not None
    assert match.family == family
    assert match.command == command


@pytest.mark.parametrize(
    ("agent", "tool", "preview"),
    [
        ("Gemini CLI", "user_prompt", '{"prompt":"/loop run tests"}'),
        ("Windsurf", "user_prompt", '{"prompt":"/review-pr"}'),
        ("OpenCode", "user_prompt", '{"prompt":"/test"}'),
        ("Gemini CLI", "functions.create_goal", '{"objective":"run forever"}'),
        ("Claude Code", "apply_patch", '{"patch":"Document /loop and /goal"}'),
        ("OpenAI Codex CLI", "user_prompt", '{"prompt":"/goal clear"}'),
        ("Claude Code", "CronCreate", '{"prompt":"remind me","recurring":false}'),
        ("Claude Code", "CronCreate", '{"prompt":"ambiguous reminder"}'),
    ],
)
def test_registry_rejects_manual_unsupported_or_incidental_commands(
    agent: str,
    tool: str,
    preview: str,
) -> None:
    assert (
        RecurringCommandRegistry().classify(
            agent_name=agent,
            tool_name=tool,
            preview=preview,
        )
        is None
    )
