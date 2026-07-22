from __future__ import annotations

import json

import pytest

from reflect.session_adapters import (
    DEFAULT_SESSION_ADAPTERS,
    ClaudeConversationAdapter,
    SessionConversationAdapterRegistry,
)


def _write_jsonl(path, records):
    path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("agent", "filename", "payload", "expected_response"),
    [
        (
            "claude",
            "claude.jsonl",
            [
                {
                    "type": "user",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "message": {"content": "Fix it"},
                },
                {
                    "type": "assistant",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "message": {
                        "model": "claude-sonnet-4-6",
                        "content": [{"type": "text", "text": "Fixed from Claude"}],
                        "usage": {"input_tokens": 10, "output_tokens": 5},
                    },
                },
            ],
            "Fixed from Claude",
        ),
        (
            "codex",
            "codex.jsonl",
            [
                {"type": "session_meta", "payload": {"id": "sess-1", "model": "gpt-5"}},
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Fix it"}],
                    },
                },
                {
                    "type": "response_item",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Fixed from Codex"}],
                    },
                },
            ],
            "Fixed from Codex",
        ),
        (
            "copilot",
            "copilot.jsonl",
            [
                {
                    "type": "user.message",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "data": {"content": "Fix it"},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "data": {"content": "Fixed from Copilot"},
                },
            ],
            "Fixed from Copilot",
        ),
        (
            "cursor",
            "cursor.jsonl",
            [
                {"role": "user", "message": {"content": [{"type": "text", "text": "Fix it"}]}},
                {
                    "role": "assistant",
                    "message": {"content": [{"type": "text", "text": "Fixed from Cursor"}]},
                },
            ],
            "Fixed from Cursor",
        ),
    ],
)
def test_jsonl_adapters_preserve_assistant_responses(
    tmp_path, agent, filename, payload, expected_response
):
    source = _write_jsonl(tmp_path / filename, payload)

    transcript = DEFAULT_SESSION_ADAPTERS.load("sess-1", agent, source)

    responses = [event for event in transcript.events if event.type == "response"]
    assert [event.content for event in responses] == [expected_response]


def test_gemini_adapter_preserves_assistant_response(tmp_path):
    source = tmp_path / "gemini.json"
    source.write_text(
        json.dumps(
            {
                "sessionId": "sess-1",
                "messages": [
                    {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "content": "Fix it"},
                    {
                        "type": "gemini",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "content": "Fixed from Gemini",
                        "model": "gemini-2.5-pro",
                        "tokens": {"input": 10, "output": 5},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    transcript = DEFAULT_SESSION_ADAPTERS.load("sess-1", "gemini", source)

    response = next(event for event in transcript.events if event.type == "response")
    assert response.content == "Fixed from Gemini"
    assert response.output_tokens == 5


def test_claude_adapter_pairs_tool_calls_and_results(tmp_path):
    source = _write_jsonl(
        tmp_path / "claude.jsonl",
        [
            {
                "type": "assistant",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tool-1",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool-1",
                            "content": "contents",
                            "is_error": False,
                        }
                    ]
                },
            },
        ],
    )

    transcript = DEFAULT_SESSION_ADAPTERS.load("sess-1", "claude", source)

    assert [event.type for event in transcript.events] == ["tool_call", "tool_result"]
    assert transcript.events[1].tool_name == "Read"
    assert transcript.events[1].success is True


def test_adapter_registry_rejects_duplicates():
    registry = SessionConversationAdapterRegistry((ClaudeConversationAdapter(),))

    with pytest.raises(ValueError, match="already registered"):
        registry.register(ClaudeConversationAdapter())


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("claude-code", "claude"),
        ("gemini-cli", "gemini"),
        ("github-copilot", "copilot"),
        ("openai-codex", "codex"),
    ],
)
def test_default_registry_supports_common_agent_aliases(alias, canonical):
    assert DEFAULT_SESSION_ADAPTERS.supports(alias)
    assert canonical in DEFAULT_SESSION_ADAPTERS.supported_agents()
