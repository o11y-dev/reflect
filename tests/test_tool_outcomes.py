import json

import pytest

from reflect.tool_outcomes import tool_failure

SCRIPT_FAILURE = [
    {"type": "input_text", "text": "Script failed\nWall time 0.0 seconds\nOutput:\n"},
    {"type": "input_text", "text": "Script error:\nexec_command failed: Too many open files (os error 24)"},
]


@pytest.mark.parametrize("output,kind,message", [
    (SCRIPT_FAILURE, "execution_error", "Too many open files"),
    (json.dumps(SCRIPT_FAILURE), "execution_error", "Too many open files"),
    ("\n".join(b["text"] for b in SCRIPT_FAILURE), "execution_error", "Too many open files"),
    ({"isError": True, "content": [{"type": "text", "text": "Connection refused"}]}, "tool_error", "Connection refused"),
    ({"is_error": True, "content": "Permission denied"}, "tool_error", "Permission denied"),
    ({"exit_code": 2, "output": "bad arguments"}, "process_exit", "code 2"),
])
def test_recognizes_outer_execution_outcomes(output, kind, message):
    failure = tool_failure("PostToolUse", {"tool.output": output})
    assert failure.error_type == kind
    assert message in failure.message


@pytest.mark.parametrize("output", [
    "The log says Too many open files (os error 24)",
    "Script failed is the text to search for",
    [{"type": "text", "text": "Quoted example:\n" + json.dumps(SCRIPT_FAILURE)}],
    [{"type": "input_text", "text": "Script completed\nWall time 0.1 seconds\nOutput:\n"},
     {"type": "input_text", "text": json.dumps(SCRIPT_FAILURE)}],
    {"isError": False, "content": [{"type": "text", "text": "error: example"}]},
    {"exit_code": 0, "output": json.dumps(SCRIPT_FAILURE)},
    {"exit_code": True},
    {"exit_code": None},
    {"is_error": "false"},
    {"data": {"isError": True}},
    {"status": "error", "count": 10},
    '[{"truncated":', None, 12,
])
def test_does_not_infer_failures_from_application_output(output):
    assert tool_failure("PostToolUse", {"tool.output": output}) is None


@pytest.mark.parametrize("event", ["PreToolUse", "BeforeMCPExecution", "Stop", "UserPromptSubmit"])
def test_only_classifies_result_events(event):
    assert tool_failure(event, {"tool.output": SCRIPT_FAILURE}) is None


@pytest.mark.parametrize("status", ["ok", "success", "completed", "skipped"])
def test_explicit_provider_success_or_skip_precedes_output(status):
    assert tool_failure("PostToolUse", {"status": status, "tool.output": SCRIPT_FAILURE}) is None


def test_preserves_explicit_failure_details():
    failure = tool_failure("PostToolUse", {
        "gen_ai.client.status": "error", "error.type": "PermissionError",
        "error.message": "Denied", "tool.output": {"exit_code": 0},
    })
    assert failure.error_type == "PermissionError"
    assert failure.message == "Denied"


def test_mcp_and_shell_execution_attributes():
    for event in ("AfterMCPExecution", "AfterShellExecution"):
        assert tool_failure(event, {"process.exit.code": 3}).error_type == "process_exit"
