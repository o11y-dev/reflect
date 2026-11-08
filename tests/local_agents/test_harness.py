"""Fast contract tests for the machine-only agent adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from reflect.context import ReflectContextService
from reflect.mcp_clients import local_mcp_agent_names
from reflect.store.sqlite import connect_sqlite

from .harness import (
    AGENT_ADAPTERS,
    BLOG_DRAFT_CLAIM,
    BLOG_DRAFT_PROOF,
    BLOG_DRAFT_TITLE,
    BLOG_REVISION_CLAIM,
    BLOG_REVISION_PROOF,
    BLOG_REVISION_TITLE,
    EFFECTIVENESS_QUESTION,
    AgentTestContext,
    AntigravityAdapter,
    ClaudeAdapter,
    CodexAdapter,
    CopilotAdapter,
    CursorAdapter,
    OpenCodeAdapter,
    blog_draft_prompt,
    blog_memory_content,
    blog_revision_prompt,
    effectiveness_task_prompt,
    extract_final_message,
    retain_session_otlp,
    score_effectiveness,
    seed_effectiveness_workflow,
    validate_blog_draft,
    validate_blog_revision,
)


def test_otlp_capture_selects_identity_not_quoted_session_text(tmp_path):
    source = tmp_path / "raw.jsonl"
    def span(session_id, text):
        return {"name": "test", "attributes": [
            {"key": "gen_ai.client.session_id", "value": {"stringValue": session_id}},
            {"key": "gen_ai.client.tool.output", "value": {"stringValue": text}},
        ]}
    source.write_text(json.dumps({"resourceSpans": [{"resource": {}, "scopeSpans": [{"spans": [
        span("selected", "result"), span("other", "selected"),
    ]}]}]}) + "\n")
    retained = retain_session_otlp({"traces": [source]}, tmp_path / "captured", "selected")
    payload = json.loads(retained["traces"].read_text())
    assert len(payload["resourceSpans"][0]["scopeSpans"][0]["spans"]) == 1
    assert "other" not in retained["traces"].read_text()


@pytest.fixture
def context(tmp_path: Path) -> AgentTestContext:
    return AgentTestContext(
        agent_name="test-agent",
        executable="/usr/local/bin/test-agent",
        workspace=tmp_path,
        db_path=tmp_path / "reflect.db",
        python_executable="/opt/reflect/bin/python",
    )


@pytest.mark.parametrize("adapter", [CodexAdapter(), ClaudeAdapter(), CursorAdapter()], ids=lambda adapter: adapter.name)
def test_telemetry_profiles_preserve_capture_and_resume(adapter, context):
    from dataclasses import replace

    context = replace(context, prompt_override="Run the isolated telemetry fixture.")
    initial = adapter.build_telemetry(context)
    resumed = adapter.build_telemetry(context, resume_session_id="observed-session")
    assert "--ephemeral" not in initial.argv
    assert "--no-session-persistence" not in initial.argv
    assert "--bare" not in initial.argv
    assert "observed-session" in resumed.argv
    assert not any("reflect_context" in argument or "reflect_complete" in argument for argument in initial.argv)
    assert initial.cwd == context.workspace


def test_claude_adapter_uses_strict_one_run_mcp_config(
    context: AgentTestContext,
) -> None:
    command = ClaudeAdapter().build(context)

    assert "--bare" in command.argv
    assert "--strict-mcp-config" in command.argv
    assert "--no-session-persistence" in command.argv
    assert "mcp__reflect__reflect_context,mcp__reflect__reflect_complete" in command.argv
    assert "--tools" not in command.argv
    assert "sonnet" in command.argv
    assert dict(command.env_overrides)["ENABLE_TOOL_SEARCH"] == "false"
    config = json.loads((context.workspace / "claude-mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["command"] == context.python_executable
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )
    baseline = ClaudeAdapter().build_baseline(context)
    assert "--strict-mcp-config" not in baseline.argv
    assert "--tools" in baseline.argv


def test_context_passes_explicit_runtime_identity_to_fresh_mcp_server(
    context: AgentTestContext,
) -> None:
    runtime_context = AgentTestContext(
        agent_name=context.agent_name,
        executable=context.executable,
        workspace=context.workspace,
        db_path=context.db_path,
        python_executable=context.python_executable,
        runtime_session_id="blog-draft-test-agent",
    )

    assert runtime_context.stdio_server["env"]["REFLECT_SESSION_ID"] == (
        "blog-draft-test-agent"
    )


def test_codex_adapter_is_ephemeral_read_only_and_uses_inline_mcp_config(
    context: AgentTestContext,
) -> None:
    command = CodexAdapter().build(context)
    joined = " ".join(command.argv)

    assert "--ephemeral" in command.argv
    assert "--ignore-user-config" in command.argv
    assert "read-only" in command.argv
    assert "mcp_servers.reflect.command=" in joined
    assert "mcp_servers.reflect.env.REFLECT_DB_PATH=" in joined
    assert "mcp_servers.reflect" not in " ".join(
        CodexAdapter().build_baseline(context).argv
    )


def test_cursor_adapter_uses_temporary_workspace_config_and_ask_mode(
    context: AgentTestContext,
) -> None:
    command = CursorAdapter().build(context)

    assert "--mode" in command.argv
    assert "ask" in command.argv
    assert "--approve-mcps" in command.argv
    assert command.argv[command.argv.index("--output-format") + 1] == "stream-json"
    baseline = CursorAdapter().build_baseline(context)
    assert baseline.argv[baseline.argv.index("--output-format") + 1] == "stream-json"
    config = json.loads((context.workspace / ".cursor" / "mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["command"] == context.python_executable
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )
    baseline_workspace = context.workspace / "baseline"
    baseline_workspace.mkdir()
    baseline_context = AgentTestContext(
        agent_name=context.agent_name,
        executable=context.executable,
        workspace=baseline_workspace,
        db_path=baseline_workspace / "unused.db",
        python_executable=context.python_executable,
    )
    CursorAdapter().build_baseline(baseline_context)
    assert not (baseline_workspace / ".cursor" / "mcp.json").exists()


def test_antigravity_adapter_uses_workspace_mcp_config(
    context: AgentTestContext,
) -> None:
    command = AntigravityAdapter().build(context)

    assert "--dangerously-skip-permissions" in command.argv
    assert "--sandbox" in command.argv
    assert "HOME" not in dict(command.env_overrides)
    config_path = context.workspace / ".agents" / "mcp_config.json"
    config = json.loads(config_path.read_text())
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )

    AntigravityAdapter().build_baseline(context)
    baseline = json.loads(config_path.read_text())
    assert baseline == {"mcpServers": {}}


def test_copilot_adapter_uses_session_scoped_mcp_and_final_message(
    context: AgentTestContext,
) -> None:
    adapter = CopilotAdapter()
    command = adapter.build(context)

    assert "--disable-builtin-mcps" in command.argv
    assert "--no-custom-instructions" in command.argv
    assert any(item.startswith("--additional-mcp-config=@") for item in command.argv)
    assert "--available-tools=reflect" in command.argv
    assert "--allow-all-mcp-server-instructions" in command.argv
    config = json.loads((context.workspace / "copilot-mcp.json").read_text())
    assert config["mcpServers"]["reflect"]["type"] == "local"
    assert config["mcpServers"]["reflect"]["tools"] == [
        "reflect_context",
        "reflect_complete",
    ]
    assert config["mcpServers"]["reflect"]["env"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )
    assert adapter.extract_final_message(
        json.dumps({"type": "assistant_message", "content": "FINAL"})
    ) == "FINAL"
    baseline = adapter.build_baseline(context)
    assert not any(item.startswith("--additional-mcp-config") for item in baseline.argv)
    assert "--available-tools=reflect-disabled(noop)" in baseline.argv


def test_opencode_adapter_disables_other_tools_and_uses_project_mcp(
    context: AgentTestContext,
) -> None:
    adapter = OpenCodeAdapter()
    command = adapter.build(context)

    assert "--pure" in command.argv
    assert "--dangerously-skip-permissions" in command.argv
    config = json.loads((context.workspace / "opencode.json").read_text())
    assert config["tools"] == {"*": False, "reflect_*": True}
    assert config["permission"] == {"reflect_*": "allow"}
    assert config["mcp"]["reflect"]["command"] == [
        context.python_executable,
        "-m",
        "reflect.mcp",
    ]
    assert config["mcp"]["reflect"]["environment"]["REFLECT_DB_PATH"] == str(
        context.db_path.resolve()
    )

    adapter.build_baseline(context)
    baseline = json.loads((context.workspace / "opencode.json").read_text())
    assert baseline["tools"] == {"*": False}
    assert "mcp" not in baseline


def test_local_adapter_inventory_matches_declared_headless_clients() -> None:
    assert tuple(adapter.name for adapter in AGENT_ADAPTERS) == local_mcp_agent_names()


def test_effectiveness_fixture_selects_the_approved_skill(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    db_path = tmp_path / "reflect.db"

    candidate_id = seed_effectiveness_workflow(db_path, workspace)
    with connect_sqlite(db_path) as conn:
        answer = ReflectContextService(conn).ask(
            EFFECTIVENESS_QUESTION,
            path=workspace,
        )

    assert answer.workflow_id == candidate_id
    assert "quartz-17" not in effectiveness_task_prompt()


def test_blog_prompts_hide_stage_contract_and_validators_score_outputs(tmp_path: Path) -> None:
    draft_prompt = blog_draft_prompt(tmp_path, "draft-summary")
    assert BLOG_DRAFT_PROOF not in draft_prompt
    assert BLOG_REVISION_PROOF not in draft_prompt
    assert BLOG_DRAFT_CLAIM in blog_memory_content()
    draft = (
        f"{BLOG_DRAFT_TITLE}\n\n"
        f"{BLOG_DRAFT_CLAIM}. "
        + "evidence " * 96
        + f"\n\n{BLOG_DRAFT_PROOF}"
    )
    assert validate_blog_draft(draft) == ()
    rendered_draft = draft.removeprefix("# ").replace(
        "local evidence becomes measurable improvement",
        "local evidence becomes measurable\nimprovement",
    )
    assert validate_blog_draft(rendered_draft) == ()

    revision_prompt = blog_revision_prompt(tmp_path, draft, "revision-summary")
    assert BLOG_REVISION_PROOF not in revision_prompt
    revision = (
        f"{BLOG_REVISION_TITLE}\n\n"
        f"{BLOG_REVISION_CLAIM} "
        + "verification " * 100
        + f"\n\n{BLOG_REVISION_PROOF}"
    )
    assert validate_blog_revision(revision) == ()


@pytest.mark.parametrize(
    ("stdout", "expected"),
    [
        (
            json.dumps({"type": "result", "result": "FINAL"}),
            "FINAL",
        ),
        (
            json.dumps({"response": "FINAL", "stats": {}}, indent=2),
            "FINAL",
        ),
        (
            json.dumps(
                {
                    "type": "assistant.message",
                    "data": {"phase": "final_answer", "content": "FINAL"},
                }
            ),
            "FINAL",
        ),
        (
            json.dumps(
                {
                    "event": "result",
                    "result": {"status": "SUCCESS", "response": "FINAL\n"},
                }
            ),
            "FINAL",
        ),
        (
            "\n".join(
                [
                    json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
                    json.dumps(
                        {
                            "type": "text",
                            "part": {"type": "text", "text": "FINAL"},
                        }
                    ),
                ]
            ),
            "FINAL",
        ),
        (
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "mcp_tool_call",
                                "result": "quartz-17 RFLX-VIOLET-7319 amber-4",
                            },
                        }
                    ),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "type": "agent_message",
                                "text": "ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN",
                            },
                        }
                    ),
                ]
            ),
            "ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN",
        ),
    ],
)
def test_extract_final_message_ignores_tool_payloads(stdout: str, expected: str) -> None:
    assert extract_final_message(stdout) == expected


@pytest.mark.parametrize("final_text", ["REFLECT_MCP_OK", "The MCP call failed."])
def test_cursor_final_assistant_message_overrides_concatenated_result(final_text: str) -> None:
    # Shape recorded from Cursor 2026.10.22-27dcf5f stream-json output.
    preamble = "I'll run the Reflect MCP smoke test exactly as specified."
    events = [
        {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": preamble}],
        }},
        {"type": "tool_call", "subtype": "completed", "result": "REFLECT_MCP_OK"},
        {"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": final_text}],
        }},
        {"type": "result", "subtype": "success", "result": preamble + final_text},
    ]

    assert CursorAdapter().extract_final_message("\n".join(map(json.dumps, events))) == final_text


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ROUTE=quartz-17 VERIFY=RFLX-VIOLET-7319 REJECT=amber-4", 3),
        ("ROUTE=amber-4 VERIFY=RFLX-VIOLET-7319 REJECT=quartz-17", 1),
        ("Use quartz-17, RFLX-VIOLET-7319, and reject amber-4.", 0),
        ("ROUTE=UNKNOWN VERIFY=UNKNOWN REJECT=UNKNOWN", 0),
    ],
)
def test_effectiveness_score_validates_exact_field_assignments(
    message: str,
    expected: int,
) -> None:
    assert score_effectiveness(message) == expected
