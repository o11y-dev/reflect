"""Native coding-agent session adapters for high-fidelity conversations.

Telemetry normalization intentionally reduces provider records to spans and calls.
These adapters preserve the user/assistant/tool chronology needed by session detail
without teaching the dashboard about each provider's on-disk format.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reflect.utils import _flatten_text_content, _json_dumps, _json_loads, _load_json_lines

MAX_CONTENT_CHARS = 20_000

AGENT_ALIASES = {
    "claude-code": "claude",
    "gemini-cli": "gemini",
    "github-copilot": "copilot",
    "openai-codex": "codex",
}


def _bounded(value: object, limit: int = MAX_CONTENT_CHARS) -> str:
    return str(value or "").strip()[:limit]


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return _bounded(content)
    if isinstance(content, dict):
        for key in ("text", "content", "message", "output"):
            if content.get(key) not in (None, ""):
                return _content_text(content[key])
        return _bounded(_json_dumps(content))
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {
            "text",
            "input_text",
            "output_text",
        }:
            parts.append(str(item.get("text") or item.get("content") or ""))
    return _bounded("\n".join(part for part in parts if part))


def _timestamp(record: dict[str, Any], fallback: str = "") -> str:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    return _bounded(
        record.get("timestamp")
        or payload.get("timestamp")
        or payload.get("created_at")
        or fallback,
        100,
    )


def _file_fallback_timestamp(path: Path, index: int) -> str:
    try:
        base = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:
        base = datetime(2000, 1, 1, tzinfo=UTC)
    return (base + timedelta(milliseconds=index)).isoformat()


@dataclass(slots=True)
class ConversationEvent:
    type: str
    timestamp: str = ""
    content: str = ""
    tool_name: str = ""
    tool_use_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    duration_ms: int = 0
    success: bool = True
    server: str = ""
    subagent_type: str = ""

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class ConversationTranscript:
    session_id: str
    agent: str
    events: list[ConversationEvent] = field(default_factory=list)
    source: str = "native"
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "agent": self.agent,
            "events": [event.as_dict() for event in self.events],
            "source": self.source,
            "warnings": list(self.warnings),
        }


class SessionConversationAdapter(ABC):
    """Provider-owned conversion from one native source to a transcript."""

    agent: str

    @abstractmethod
    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        raise NotImplementedError


class ClaudeConversationAdapter(SessionConversationAdapter):
    agent = "claude"

    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        events: list[ConversationEvent] = []
        tool_names: dict[str, str] = {}
        resolved_session_id = session_id
        for record in _load_json_lines(path):
            kind = str(record.get("type") or "")
            resolved_session_id = resolved_session_id or str(record.get("sessionId") or "")
            timestamp = _timestamp(record)
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            if kind == "assistant":
                text = _flatten_text_content(content)
                usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
                if text:
                    events.append(
                        ConversationEvent(
                            type="response",
                            timestamp=timestamp,
                            content=_bounded(text),
                            model=_bounded(message.get("model"), 200),
                            input_tokens=int(usage.get("input_tokens") or 0),
                            output_tokens=int(usage.get("output_tokens") or 0),
                            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                        )
                    )
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_use":
                            continue
                        tool_use_id = _bounded(item.get("id"), 300)
                        tool_name = _bounded(item.get("name"), 300)
                        if tool_use_id:
                            tool_names[tool_use_id] = tool_name
                        events.append(
                            ConversationEvent(
                                type="tool_call",
                                timestamp=timestamp,
                                content=_bounded(_json_dumps(item.get("input") or {})),
                                tool_name=tool_name,
                                tool_use_id=tool_use_id,
                            )
                        )
            elif kind == "user":
                text = (
                    _flatten_text_content(content)
                    if isinstance(content, list)
                    else _content_text(content)
                )
                if text:
                    events.append(
                        ConversationEvent(
                            type="prompt", timestamp=timestamp, content=_bounded(text)
                        )
                    )
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict) or item.get("type") != "tool_result":
                            continue
                        tool_use_id = _bounded(item.get("tool_use_id"), 300)
                        events.append(
                            ConversationEvent(
                                type="tool_result",
                                timestamp=timestamp,
                                content=_content_text(item.get("content")),
                                tool_name=tool_names.get(tool_use_id, ""),
                                tool_use_id=tool_use_id,
                                success=not bool(item.get("is_error")),
                            )
                        )
        return ConversationTranscript(resolved_session_id or path.stem, self.agent, events)


class CodexConversationAdapter(SessionConversationAdapter):
    agent = "codex"

    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        records = list(_load_json_lines(path))
        meta = next(
            (
                record.get("payload") or {}
                for record in records
                if record.get("type") == "session_meta"
            ),
            {},
        )
        resolved_session_id = session_id or str(meta.get("id") or path.stem).removeprefix(
            "rollout-"
        )
        model = _bounded(meta.get("model") or meta.get("last_known_model"), 200)
        events: list[ConversationEvent] = []
        tools: dict[str, str] = {}
        for record in records:
            if record.get("type") != "response_item":
                continue
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
            timestamp = _timestamp(record)
            item_type = str(payload.get("type") or "")
            if item_type == "message":
                role = str(payload.get("role") or "")
                text = _content_text(payload.get("content"))
                if (
                    role == "user"
                    and text
                    and not text.lstrip().startswith("<environment_context>")
                ):
                    events.append(
                        ConversationEvent(type="prompt", timestamp=timestamp, content=text)
                    )
                elif role == "assistant" and text:
                    events.append(
                        ConversationEvent(
                            type="response",
                            timestamp=timestamp,
                            content=text,
                            model=model,
                        )
                    )
            elif item_type in {"function_call", "custom_tool_call"}:
                tool_use_id = _bounded(payload.get("call_id") or payload.get("id"), 300)
                tool_name = _bounded(payload.get("name") or payload.get("tool_name"), 300)
                tools[tool_use_id] = tool_name
                events.append(
                    ConversationEvent(
                        type="tool_call",
                        timestamp=timestamp,
                        content=_content_text(payload.get("arguments") or payload.get("input")),
                        tool_name=tool_name,
                        tool_use_id=tool_use_id,
                    )
                )
            elif item_type in {"function_call_output", "custom_tool_call_output"}:
                tool_use_id = _bounded(payload.get("call_id") or payload.get("id"), 300)
                output = payload.get("output") or payload.get("content")
                output_text = _content_text(output)
                events.append(
                    ConversationEvent(
                        type="tool_result",
                        timestamp=timestamp,
                        content=output_text,
                        tool_name=tools.get(tool_use_id, _bounded(payload.get("name"), 300)),
                        tool_use_id=tool_use_id,
                        success=not bool(payload.get("error")),
                    )
                )
        return ConversationTranscript(resolved_session_id, self.agent, events)


class CopilotConversationAdapter(SessionConversationAdapter):
    agent = "copilot"

    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        events: list[ConversationEvent] = []
        tools: dict[str, str] = {}
        resolved_session_id = session_id or path.parent.name
        for record in _load_json_lines(path):
            kind = str(record.get("type") or "")
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            timestamp = _timestamp(record)
            if kind == "session.start":
                resolved_session_id = resolved_session_id or str(data.get("sessionId") or "")
            elif kind == "user.message":
                text = _content_text(data.get("content"))
                if text:
                    events.append(
                        ConversationEvent(type="prompt", timestamp=timestamp, content=text)
                    )
            elif kind == "assistant.message":
                text = _content_text(data.get("content") or data.get("message"))
                if text:
                    events.append(
                        ConversationEvent(
                            type="response",
                            timestamp=timestamp,
                            content=text,
                            model=_bounded(data.get("model"), 200),
                            output_tokens=int(data.get("outputTokens") or 0),
                        )
                    )
            elif kind == "tool.execution_start":
                tool_use_id = _bounded(data.get("toolCallId") or data.get("id"), 300)
                tool_name = _bounded(data.get("toolName"), 300)
                tools[tool_use_id] = tool_name
                events.append(
                    ConversationEvent(
                        type="tool_call",
                        timestamp=timestamp,
                        content=_bounded(_json_dumps(data.get("arguments") or {})),
                        tool_name=tool_name,
                        tool_use_id=tool_use_id,
                    )
                )
            elif kind == "tool.execution_complete":
                tool_use_id = _bounded(data.get("toolCallId") or data.get("id"), 300)
                events.append(
                    ConversationEvent(
                        type="tool_result",
                        timestamp=timestamp,
                        content=_content_text(
                            data.get("result") or data.get("output") or data.get("error")
                        ),
                        tool_name=tools.get(tool_use_id, _bounded(data.get("toolName"), 300)),
                        tool_use_id=tool_use_id,
                        success=bool(data.get("success", False)),
                    )
                )
        return ConversationTranscript(resolved_session_id, self.agent, events)


class GeminiConversationAdapter(SessionConversationAdapter):
    agent = "gemini"

    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        payload = _json_loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return ConversationTranscript(session_id or path.stem, self.agent)
        events: list[ConversationEvent] = []
        for message in payload.get("messages") or []:
            if not isinstance(message, dict):
                continue
            timestamp = _bounded(message.get("timestamp"), 100)
            kind = str(message.get("type") or "")
            if kind == "user":
                text = _content_text(message.get("content"))
                if text:
                    events.append(
                        ConversationEvent(type="prompt", timestamp=timestamp, content=text)
                    )
            elif kind == "gemini":
                text = _content_text(message.get("content"))
                tokens = message.get("tokens") if isinstance(message.get("tokens"), dict) else {}
                if text:
                    events.append(
                        ConversationEvent(
                            type="response",
                            timestamp=timestamp,
                            content=text,
                            model=_bounded(message.get("model"), 200),
                            input_tokens=int(tokens.get("input") or 0),
                            output_tokens=int(tokens.get("output") or 0),
                        )
                    )
                for index, call in enumerate(message.get("toolCalls") or []):
                    if not isinstance(call, dict):
                        continue
                    tool_use_id = _bounded(call.get("id") or f"{timestamp}:{index}", 300)
                    tool_name = _bounded(call.get("displayName") or call.get("name"), 300)
                    call_timestamp = _bounded(call.get("timestamp") or timestamp, 100)
                    events.append(
                        ConversationEvent(
                            type="tool_call",
                            timestamp=call_timestamp,
                            content=_bounded(_json_dumps(call.get("args") or {})),
                            tool_name=tool_name,
                            tool_use_id=tool_use_id,
                        )
                    )
                    if call.get("result") is not None or call.get("status") is not None:
                        events.append(
                            ConversationEvent(
                                type="tool_result",
                                timestamp=call_timestamp,
                                content=_content_text(call.get("result") or call.get("error")),
                                tool_name=tool_name,
                                tool_use_id=tool_use_id,
                                success=str(call.get("status") or "success").lower() == "success",
                            )
                        )
        return ConversationTranscript(
            session_id or str(payload.get("sessionId") or path.stem),
            self.agent,
            events,
        )


class CursorConversationAdapter(SessionConversationAdapter):
    agent = "cursor"

    def load(self, session_id: str, path: Path) -> ConversationTranscript:
        events: list[ConversationEvent] = []
        tools: dict[str, str] = {}
        for index, record in enumerate(_load_json_lines(path)):
            role = str(record.get("role") or "")
            if role not in {"user", "assistant"}:
                continue
            timestamp = _timestamp(record, _file_fallback_timestamp(path, index))
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            text = (
                _flatten_text_content(content)
                if isinstance(content, list)
                else _content_text(content)
            )
            if role == "user" and text:
                events.append(
                    ConversationEvent(type="prompt", timestamp=timestamp, content=_bounded(text))
                )
            elif role == "assistant" and text:
                events.append(
                    ConversationEvent(
                        type="response",
                        timestamp=timestamp,
                        content=_bounded(text),
                        model=_bounded(message.get("model"), 200),
                    )
                )
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use":
                    tool_use_id = _bounded(item.get("id"), 300)
                    tool_name = _bounded(item.get("name"), 300)
                    tools[tool_use_id] = tool_name
                    events.append(
                        ConversationEvent(
                            type="tool_call",
                            timestamp=timestamp,
                            content=_bounded(_json_dumps(item.get("input") or {})),
                            tool_name=tool_name,
                            tool_use_id=tool_use_id,
                        )
                    )
                elif item.get("type") == "tool_result":
                    tool_use_id = _bounded(item.get("tool_use_id"), 300)
                    events.append(
                        ConversationEvent(
                            type="tool_result",
                            timestamp=timestamp,
                            content=_content_text(item.get("content")),
                            tool_name=tools.get(tool_use_id, ""),
                            tool_use_id=tool_use_id,
                            success=not bool(item.get("is_error")),
                        )
                    )
        return ConversationTranscript(session_id or path.stem, self.agent, events)


class SessionConversationAdapterRegistry:
    def __init__(self, adapters: tuple[SessionConversationAdapter, ...] = ()) -> None:
        self._adapters: dict[str, SessionConversationAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: SessionConversationAdapter, *, replace: bool = False) -> None:
        agent = adapter.agent.strip().lower()
        if not agent:
            raise ValueError("A session conversation adapter requires an agent name")
        if agent in self._adapters and not replace:
            raise ValueError(f"Session conversation adapter '{agent}' is already registered")
        self._adapters[agent] = adapter

    def supports(self, agent: str) -> bool:
        return self._agent_key(agent) in self._adapters

    def supported_agents(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))

    def load(self, session_id: str, agent: str, path: Path) -> ConversationTranscript:
        normalized_agent = self._agent_key(agent)
        adapter = self._adapters.get(normalized_agent)
        if adapter is None:
            raise ValueError(f"Session detail loading is not implemented for agent '{agent}' yet.")
        return adapter.load(session_id, path)

    @staticmethod
    def _agent_key(agent: str) -> str:
        normalized = agent.strip().lower()
        return AGENT_ALIASES.get(normalized, normalized)


DEFAULT_SESSION_ADAPTERS = SessionConversationAdapterRegistry(
    (
        ClaudeConversationAdapter(),
        CodexConversationAdapter(),
        CopilotConversationAdapter(),
        CursorConversationAdapter(),
        GeminiConversationAdapter(),
    )
)


__all__ = [
    "AGENT_ALIASES",
    "ClaudeConversationAdapter",
    "CodexConversationAdapter",
    "ConversationEvent",
    "ConversationTranscript",
    "CopilotConversationAdapter",
    "CursorConversationAdapter",
    "DEFAULT_SESSION_ADAPTERS",
    "GeminiConversationAdapter",
    "SessionConversationAdapter",
    "SessionConversationAdapterRegistry",
]
