from __future__ import annotations

import hashlib
import json as _json_stdlib
import os
import re
import tomllib
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from reflect.utils import _json_dumps, _json_loads, logger

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))
HOOK_HOME = Path(os.environ.get("IDE_OTEL_HOOK_HOME", Path.home() / ".local" / "share" / "opentelemetry-hooks"))


def _flatten_otlp_attributes(otlp_attrs: list[dict]) -> dict:
    """Convert OTLP attribute list to flat dict.

    OTLP format: [{"key": "k", "value": {"stringValue": "v"}}, ...]
    Output:      {"k": "v", ...}
    """
    flat: dict = {}
    for attr in otlp_attrs:
        key = attr.get("key", "")
        value_obj = attr.get("value", {})
        if "stringValue" in value_obj:
            flat[key] = value_obj["stringValue"]
        elif "intValue" in value_obj:
            flat[key] = int(value_obj["intValue"])
        elif "doubleValue" in value_obj:
            flat[key] = value_obj["doubleValue"]
        elif "boolValue" in value_obj:
            flat[key] = value_obj["boolValue"]
        elif "arrayValue" in value_obj:
            flat[key] = value_obj["arrayValue"]
    return flat


def _load_otlp_traces(file_path: Path, since_ns: int = 0) -> Iterable[dict]:
    """Load spans from an OTLP JSON file (collector file exporter format).

    Flattens the nested resourceSpans → scopeSpans → spans structure
    into flat span dicts with a plain 'attributes' dict, matching the
    format that analyze_telemetry expects.

    When *since_ns* is set, spans older than the cutoff are skipped
    **before** attribute flattening for performance.
    """
    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = _json_loads(line)
            except (ValueError, _json_stdlib.JSONDecodeError):
                continue

            for resource_span in payload.get("resourceSpans", []):
                resource_attrs = _flatten_otlp_attributes(
                    resource_span.get("resource", {}).get("attributes", [])
                )
                for scope_span in resource_span.get("scopeSpans", []):
                    for span in scope_span.get("spans", []):
                        start_ns = int(span.get("startTimeUnixNano", 0))
                        if since_ns and start_ns and start_ns < since_ns:
                            continue
                        flat_span = {
                            "name": span.get("name", ""),
                            "traceId": span.get("traceId", ""),
                            "spanId": span.get("spanId", ""),
                            "parentSpanId": span.get("parentSpanId", ""),
                            "start_time_ns": start_ns,
                            "end_time_ns": int(span.get("endTimeUnixNano", 0)),
                            "attributes": {
                                **resource_attrs,
                                **_flatten_otlp_attributes(span.get("attributes", [])),
                            },
                        }
                        if _is_low_level_codex_span(flat_span):
                            continue
                        yield flat_span


def _is_low_level_codex_span(span: dict) -> bool:
    """Return true for noisy native Codex runtime spans.

    Codex emits useful session/model/tool records as OTLP logs today. Its trace
    stream is dominated by Rust HTTP/runtime internals (`h2`, `hyper`, etc.)
    under `codex_cli_rs` / `codex-app-server`, which would otherwise swamp the
    agent dashboard with transport spans.
    """
    attrs = span.get("attributes") or {}
    service = str(attrs.get("service.name") or "").lower()
    if service not in {"codex_cli_rs", "codex-app-server"}:
        return False
    useful_keys = {
        "gen_ai.client.hook.event",
        "gen_ai.client.session_id",
        "session.id",
        "conversation.id",
        "event.name",
    }
    return not any(attrs.get(key) for key in useful_keys)


def _load_otlp_logs(file_path: Path) -> Iterable[dict]:
    """Load log records from an OTLP JSON logs file."""
    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = _json_loads(line)
            except _json_stdlib.JSONDecodeError:
                continue

            for resource_log in payload.get("resourceLogs", []):
                resource_attrs = _flatten_otlp_attributes(
                    resource_log.get("resource", {}).get("attributes", [])
                )
                for scope_log in resource_log.get("scopeLogs", []):
                    for record in scope_log.get("logRecords", []):
                        body = record.get("body", {})
                        body_value = None
                        if isinstance(body, dict) and body:
                            body_value = next(iter(body.values()))
                        yield {
                            "time_ns": int(record.get("timeUnixNano", 0)),
                            "observed_time_ns": int(record.get("observedTimeUnixNano", 0) or 0),
                            "severity_text": record.get("severityText", ""),
                            "severity_number": int(record.get("severityNumber", 0) or 0),
                            "trace_id": record.get("traceId", ""),
                            "span_id": record.get("spanId", ""),
                            "body": body_value,
                            "attributes": {
                                **resource_attrs,
                                **_flatten_otlp_attributes(record.get("attributes", [])),
                            },
                        }


def _coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _coerce_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _usage_attrs_from_token_count(info: dict) -> dict[str, int]:
    usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
    if not isinstance(usage, dict):
        return {}
    input_tokens = _coerce_int(usage.get("input_tokens"))
    cached_tokens = _coerce_int(usage.get("cached_input_tokens"))
    output_tokens = _coerce_int(usage.get("output_tokens"))
    reasoning_tokens = _coerce_int(usage.get("reasoning_output_tokens"))
    attrs: dict[str, int] = {}
    if input_tokens or cached_tokens:
        attrs["gen_ai.usage.input_tokens"] = max(input_tokens - cached_tokens, 0)
        attrs["gen_ai.usage.cache_read.input_tokens"] = cached_tokens
    if output_tokens:
        attrs["gen_ai.usage.output_tokens"] = output_tokens
    if reasoning_tokens:
        attrs["gen_ai.usage.reasoning_output_tokens"] = reasoning_tokens
    return attrs


def _first_attr(attrs: dict, *names: str) -> str:
    for name in names:
        value = attrs.get(name)
        if value not in (None, ""):
            return str(value)
    return ""


def _load_codex_default_model() -> str:
    config_path = Path.home() / ".codex" / "config.toml"
    try:
        data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    model = data.get("model")
    return model.strip() if isinstance(model, str) else ""


def _iter_codex_log_spans(records: Iterable[dict], since_ns: int = 0) -> Iterable[dict]:
    """Normalize native Codex OTLP log records into Reflect hook-like spans."""
    active_tools: dict[tuple[str, str], dict] = {}
    for index, record in enumerate(records):
        attrs = record.get("attributes") or {}
        service = str(attrs.get("service.name") or "")
        if service not in {"codex_cli_rs", "codex-app-server"}:
            continue

        event_name = str(attrs.get("event.name") or "")
        if not event_name.startswith("codex."):
            continue

        session_id = str(attrs.get("conversation.id") or "").strip()
        if not session_id:
            continue

        ts_ns = _parse_timestamp_to_ns(attrs.get("event.timestamp")) or int(
            record.get("time_ns", 0) or record.get("observed_time_ns", 0) or 0
        )
        if since_ns and ts_ns and ts_ns < since_ns:
            continue

        trace_id = _stable_hex_id("codex", session_id, length=32)
        base_attrs = {
            "gen_ai.client.name": "codex",
            "gen_ai.provider.name": "openai",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "service.name": service,
        }
        model = str(attrs.get("slug") or attrs.get("model") or "").strip()
        if model:
            base_attrs["gen_ai.request.model"] = model

        if event_name == "codex.conversation_starts":
            yield _make_flat_span(
                "gen_ai.client.hook.SessionStart",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "SessionStart",
                },
                trace_id,
                f"{index}:conversation_starts",
            )
        elif event_name == "codex.user_prompt":
            yield _make_flat_span(
                "gen_ai.client.hook.UserPromptSubmit",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "UserPromptSubmit",
                    "gen_ai.client.prompt": str(attrs.get("prompt") or ""),
                },
                trace_id,
                f"{index}:user_prompt",
            )
        elif event_name == "codex.tool_decision":
            tool_name = str(attrs.get("tool_name") or "")
            call_id = str(attrs.get("call_id") or f"{index}")
            active_tools[(session_id, call_id)] = {
                "start_ns": ts_ns,
                "tool_name": tool_name,
            }
            yield _make_flat_span(
                "gen_ai.client.hook.PreToolUse",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "PreToolUse",
                    "gen_ai.client.tool_name": tool_name,
                    "gen_ai.client.tool_use_id": call_id,
                    "gen_ai.client.tool.input": _json_dumps(
                        {
                            "decision": attrs.get("decision", ""),
                            "source": attrs.get("source", ""),
                        }
                    ),
                },
                trace_id,
                f"{index}:tool_decision:{call_id}",
            )
        elif event_name == "codex.tool_result":
            tool_name = str(attrs.get("tool_name") or "")
            call_id = str(attrs.get("call_id") or f"{index}")
            start_info = active_tools.pop((session_id, call_id), {})
            duration_ms = _coerce_int(attrs.get("duration_ms"))
            start_ns = max(ts_ns - duration_ms * 1_000_000, 0) if duration_ms else int(
                start_info.get("start_ns") or ts_ns
            )
            end_ns = max(ts_ns, start_ns)
            success = _coerce_bool(attrs.get("success"))
            hook_event = "PostToolUse" if success else "PostToolUseFailure"
            yield _make_flat_span(
                f"gen_ai.client.hook.{hook_event}",
                start_ns,
                end_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": hook_event,
                    "gen_ai.client.tool_name": tool_name or str(start_info.get("tool_name") or ""),
                    "gen_ai.client.tool_use_id": call_id,
                    "gen_ai.client.tool.input": str(attrs.get("arguments") or ""),
                },
                trace_id,
                f"{index}:tool_result:{call_id}",
            )
        elif event_name == "codex.sse_event" and attrs.get("event.kind") == "response.completed":
            input_tokens = _coerce_int(attrs.get("input_token_count"))
            cached_tokens = _coerce_int(attrs.get("cached_token_count"))
            output_tokens = _coerce_int(attrs.get("output_token_count"))
            yield _make_flat_span(
                "gen_ai.client.hook.Stop",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "Stop",
                    "gen_ai.usage.input_tokens": max(input_tokens - cached_tokens, 0),
                    "gen_ai.usage.output_tokens": output_tokens,
                    "gen_ai.usage.cache_read.input_tokens": cached_tokens,
                },
                trace_id,
                f"{index}:sse_response_completed",
            )


def _iter_claude_log_spans(records: Iterable[dict], since_ns: int = 0) -> Iterable[dict]:
    """Normalize native Claude Code OTLP log records into Reflect hook-like spans."""
    for index, record in enumerate(records):
        attrs = record.get("attributes") or {}
        service = str(attrs.get("service.name") or "")
        event_name = str(record.get("body") or attrs.get("event.name") or "")
        if service != "claude-code":
            continue
        if not event_name.startswith("claude_code."):
            event_name = f"claude_code.{event_name}"

        session_id = str(attrs.get("session.id") or attrs.get("gen_ai.client.session_id") or "").strip()
        if not session_id:
            continue

        ts_ns = _parse_timestamp_to_ns(attrs.get("event.timestamp")) or int(
            record.get("time_ns", 0) or record.get("observed_time_ns", 0) or 0
        )
        if since_ns and ts_ns and ts_ns < since_ns:
            continue

        trace_id = _stable_hex_id("claude", session_id, length=32)
        base_attrs = {
            "gen_ai.client.name": "claude",
            "gen_ai.system": "claude",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "service.name": service,
        }
        if attrs.get("model"):
            base_attrs["gen_ai.request.model"] = str(attrs["model"])

        if event_name == "claude_code.user_prompt":
            yield _make_flat_span(
                "gen_ai.client.hook.UserPromptSubmit",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "UserPromptSubmit",
                    "gen_ai.client.prompt": str(attrs.get("prompt") or "[REDACTED]"),
                },
                trace_id,
                f"{index}:user_prompt",
            )
        elif event_name == "claude_code.api_request":
            duration_ms = _coerce_int(attrs.get("duration_ms"))
            start_ns = max(ts_ns - duration_ms * 1_000_000, 0) if duration_ms else ts_ns
            span_attrs = {
                **base_attrs,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.client.status": "ok" if str(attrs.get("success") or "true").lower() != "false" else "error",
                "gen_ai.usage.input_tokens": _coerce_int(attrs.get("input_tokens")),
                "gen_ai.usage.output_tokens": _coerce_int(attrs.get("output_tokens")),
                "gen_ai.usage.cache_read.input_tokens": _coerce_int(attrs.get("cache_read_tokens")),
                "gen_ai.usage.cache_creation.input_tokens": _coerce_int(attrs.get("cache_creation_tokens")),
            }
            cost = attrs.get("cost_usd")
            if cost not in (None, ""):
                span_attrs["gen_ai.usage.cost_usd"] = str(cost)
            yield _make_flat_span(
                "gen_ai.client.hook.Stop",
                start_ns,
                ts_ns,
                span_attrs,
                trace_id,
                f"{index}:api_request",
            )


def _iter_gemini_log_spans(records: Iterable[dict], since_ns: int = 0) -> Iterable[dict]:
    """Normalize Gemini CLI OTLP log records into Reflect hook-like spans."""
    for index, record in enumerate(records):
        attrs = record.get("attributes") or {}
        if str(attrs.get("service.name") or "") != "gemini-cli":
            continue

        session_id = str(attrs.get("session.id") or attrs.get("gen_ai.client.session_id") or "").strip()
        if not session_id:
            continue

        ts_ns = _parse_timestamp_to_ns(attrs.get("event.timestamp")) or int(
            record.get("time_ns", 0) or record.get("observed_time_ns", 0) or 0
        )
        if since_ns and ts_ns and ts_ns < since_ns:
            continue

        event_name = str(attrs.get("event.name") or "")
        trace_id = _stable_hex_id("gemini", session_id, length=32)
        base_attrs = {
            "gen_ai.client.name": "gemini",
            "gen_ai.system": "google",
            "gen_ai.provider.name": "google",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "service.name": "gemini-cli",
        }

        model = str(
            attrs.get("gen_ai.request.model")
            or attrs.get("model")
            or attrs.get("model_name")
            or attrs.get("decision_model")
            or ""
        ).strip()
        if model:
            base_attrs["gen_ai.request.model"] = model

        if event_name == "gemini_cli.hook_call":
            hook_event = str(attrs.get("hook_event_name") or "").strip()
            if not hook_event:
                continue
            duration_ms = _coerce_int(attrs.get("duration_ms"))
            start_ns = max(ts_ns - duration_ms * 1_000_000, 0) if duration_ms else ts_ns
            status = "ok" if _coerce_bool(attrs.get("success")) else "error"
            span_attrs = {
                **base_attrs,
                "gen_ai.client.hook.event": hook_event,
                "gen_ai.client.status": status,
            }
            yield _make_flat_span(
                f"gen_ai.client.hook.{hook_event}",
                start_ns,
                ts_ns,
                span_attrs,
                trace_id,
                f"{index}:hook_call:{hook_event}",
            )
        elif event_name == "gemini_cli.user_prompt":
            yield _make_flat_span(
                "gen_ai.client.hook.UserPromptSubmit",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "UserPromptSubmit",
                    "gen_ai.client.prompt": "[REDACTED]",
                },
                trace_id,
                f"{index}:user_prompt",
            )
        elif event_name == "gemini_cli.api_response":
            duration_ms = _coerce_int(attrs.get("duration_ms"))
            start_ns = max(ts_ns - duration_ms * 1_000_000, 0) if duration_ms else ts_ns
            yield _make_flat_span(
                "gen_ai.client.hook.Stop",
                start_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "Stop",
                    "gen_ai.client.status": "ok",
                    "gen_ai.usage.input_tokens": _coerce_int(attrs.get("input_token_count")),
                    "gen_ai.usage.output_tokens": _coerce_int(attrs.get("output_token_count")),
                    "gen_ai.usage.cache_read.input_tokens": _coerce_int(attrs.get("cached_content_token_count")),
                    "gen_ai.usage.reasoning_output_tokens": _coerce_int(attrs.get("thoughts_token_count")),
                },
                trace_id,
                f"{index}:api_response",
            )
        elif event_name == "gemini_cli.api_error":
            duration_ms = _coerce_int(attrs.get("duration_ms") or attrs.get("duration"))
            start_ns = max(ts_ns - duration_ms * 1_000_000, 0) if duration_ms else ts_ns
            yield _make_flat_span(
                "gen_ai.client.hook.Stop",
                start_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "Stop",
                    "gen_ai.client.status": "error",
                    "error.type": str(attrs.get("error.type") or ""),
                    "error.message": str(attrs.get("error.message") or attrs.get("error") or "")[:500],
                },
                trace_id,
                f"{index}:api_error",
            )


def _load_json_lines(file_path: Path) -> Iterable[dict]:
    with file_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = _json_loads(line)
            except (ValueError, _json_stdlib.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield payload


def _parse_timestamp_to_ns(value) -> int:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        ivalue = int(value)
        return ivalue if ivalue > 10**15 else ivalue * 1_000_000
    if not isinstance(value, str):
        return 0
    text = value.strip()
    if not text:
        return 0
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(text).timestamp() * 1_000_000_000)
    except ValueError:
        return 0


def _stable_hex_id(*parts: str, length: int = 16) -> str:
    digest = hashlib.sha1("::".join(str(p) for p in parts).encode("utf-8")).hexdigest()
    return digest[:length]


def _make_flat_span(
    name: str,
    start_ns: int,
    end_ns: int,
    attributes: dict,
    trace_id: str,
    span_id_seed: str,
    parent_span_id: str = "",
) -> dict:
    return {
        "name": name,
        "traceId": trace_id,
        "spanId": _stable_hex_id(trace_id, span_id_seed, length=16),
        "parentSpanId": parent_span_id,
        "start_time_ns": start_ns,
        "end_time_ns": end_ns if end_ns >= start_ns else start_ns,
        "attributes": attributes,
    }


def _flatten_text_content(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _codex_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n".join(part for part in parts if part)


def _codex_record_timestamp_ns(record: dict) -> int:
    return _parse_timestamp_to_ns(
        record.get("timestamp")
        or (record.get("payload") or {}).get("timestamp")
        or (record.get("payload") or {}).get("created_at")
    )


def _cursor_tool_attrs(tool_name: str, tool_input: object) -> dict:
    attrs = {
        "gen_ai.client.tool_name": tool_name,
        "gen_ai.client.tool.input": tool_input if isinstance(tool_input, str) else _json_dumps(tool_input or {}),
    }
    if isinstance(tool_input, dict) and tool_name == "CallMcpTool":
        server = tool_input.get("server")
        mcp_tool = tool_input.get("toolName") or tool_input.get("tool")
        if isinstance(server, str) and server.strip():
            attrs["gen_ai.client.mcp_server"] = server.strip()
        if isinstance(mcp_tool, str) and mcp_tool.strip():
            attrs["gen_ai.client.mcp_tool"] = mcp_tool.strip()
    return attrs


def _discover_rich_session_files() -> list[tuple[str, Path]]:
    home = Path.home()
    candidates: list[tuple[str, Path]] = []
    candidates.extend(("codex", p) for p in sorted((home / ".codex" / "sessions").glob("**/*.jsonl")))
    candidates.extend(("copilot", p) for p in sorted((home / ".copilot" / "session-state").glob("*/events.jsonl")))
    candidates.extend(("cursor", p) for p in sorted((home / ".cursor" / "projects").glob("**/agent-transcripts/**/*.jsonl")))
    candidates.extend(("claude", p) for p in sorted((home / ".claude" / "projects").glob("**/*.jsonl")))
    candidates.extend(("gemini", p) for p in sorted((home / ".gemini" / "tmp").glob("**/chats/session-*.json")))
    return candidates


def _iter_codex_session_spans(file_path: Path) -> Iterable[dict]:
    records = list(_load_json_lines(file_path))
    if not records:
        return

    meta = next((r.get("payload") or {} for r in records if r.get("type") == "session_meta"), {})
    session_id = str(meta.get("id") or file_path.stem).removeprefix("rollout-")
    trace_id = _stable_hex_id("codex", session_id, length=32)
    base_attrs = {
        "gen_ai.client.name": "codex",
        "gen_ai.provider.name": "openai",
        "gen_ai.client.session_id": session_id,
        "session.id": session_id,
        "service.name": "codex",
    }
    codex_model = str(meta.get("model") or meta.get("last_known_model") or "").strip()
    if not codex_model:
        codex_model = _load_codex_default_model()
        if codex_model:
            base_attrs["gen_ai.request.model_source"] = "codex_config_default"
    if codex_model:
        base_attrs["gen_ai.request.model"] = codex_model
    if meta.get("model_provider"):
        base_attrs["gen_ai.system"] = str(meta["model_provider"])
    if meta.get("cwd"):
        base_attrs["code.workspace.root"] = str(meta["cwd"])

    first_ts = 0
    last_ts = 0
    active_tools: dict[str, dict] = {}

    for index, record in enumerate(records):
        ts_ns = _codex_record_timestamp_ns(record)
        if ts_ns:
            first_ts = ts_ns if not first_ts else min(first_ts, ts_ns)
            last_ts = max(last_ts, ts_ns)
        record_type = record.get("type")
        payload = record.get("payload") or {}

        if record_type == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                role = payload.get("role")
                text = _codex_content_text(payload.get("content"))
                if role == "user":
                    if not text or text.lstrip().startswith("<environment_context>"):
                        continue
                    yield _make_flat_span(
                        "gen_ai.client.hook.UserPromptSubmit",
                        ts_ns,
                        ts_ns,
                        {
                            **base_attrs,
                            "gen_ai.client.hook.event": "UserPromptSubmit",
                            "gen_ai.client.prompt": text,
                        },
                        trace_id,
                        f"{index}:user",
                    )
                elif role == "assistant":
                    span_attrs = {
                        **base_attrs,
                        "gen_ai.client.hook.event": "Stop",
                    }
                    if text:
                        span_attrs["gen_ai.client.output"] = text
                    yield _make_flat_span(
                        "gen_ai.client.hook.Stop",
                        ts_ns,
                        ts_ns,
                        span_attrs,
                        trace_id,
                        f"{index}:assistant",
                    )
            elif item_type == "function_call":
                call_id = str(payload.get("call_id") or payload.get("id") or f"{index}")
                tool_name = str(payload.get("name") or "")
                arguments = payload.get("arguments")
                active_tools[call_id] = {
                    "start_ns": ts_ns,
                    "tool_name": tool_name,
                }
                yield _make_flat_span(
                    "gen_ai.client.hook.PreToolUse",
                    ts_ns,
                    ts_ns,
                    {
                        **base_attrs,
                        "gen_ai.client.hook.event": "PreToolUse",
                        "gen_ai.client.tool_name": tool_name,
                        "gen_ai.client.tool_use_id": call_id,
                        "gen_ai.client.tool.input": arguments if isinstance(arguments, str) else _json_dumps(arguments or {}),
                    },
                    trace_id,
                    f"{index}:function_call:{call_id}",
                )
            elif item_type == "function_call_output":
                call_id = str(payload.get("call_id") or payload.get("id") or f"{index}")
                start_info = active_tools.pop(call_id, {})
                output = payload.get("output")
                yield _make_flat_span(
                    "gen_ai.client.hook.PostToolUse",
                    int(start_info.get("start_ns") or ts_ns),
                    ts_ns,
                    {
                        **base_attrs,
                        "gen_ai.client.hook.event": "PostToolUse",
                        "gen_ai.client.tool_name": str(start_info.get("tool_name") or payload.get("name") or ""),
                        "gen_ai.client.tool_use_id": call_id,
                        "gen_ai.client.tool.output": output if isinstance(output, str) else _json_dumps(output or {}),
                    },
                    trace_id,
                    f"{index}:function_call_output:{call_id}",
                )
        elif record_type == "event_msg" and payload.get("type") == "token_count":
            usage_attrs = _usage_attrs_from_token_count(payload.get("info") or {})
            if not usage_attrs:
                continue
            yield _make_flat_span(
                "gen_ai.client.hook.Stop",
                ts_ns,
                ts_ns,
                {
                    **base_attrs,
                    "gen_ai.client.hook.event": "Stop",
                    **usage_attrs,
                },
                trace_id,
                f"{index}:token_count",
            )

    if first_ts:
        yield _make_flat_span(
            "gen_ai.client.hook.SessionStart",
            first_ts,
            first_ts,
            {
                **base_attrs,
                "gen_ai.client.hook.event": "SessionStart",
            },
            trace_id,
            "synthetic:session.start",
        )
    if last_ts:
        yield _make_flat_span(
            "gen_ai.client.hook.SessionEnd",
            last_ts,
            last_ts,
            {
                **base_attrs,
                "gen_ai.client.hook.event": "SessionEnd",
            },
            trace_id,
            "synthetic:session.end",
        )


def _iter_copilot_session_spans(file_path: Path) -> Iterable[dict]:
    events = list(_load_json_lines(file_path))
    if not events:
        return
    session_start = next((e for e in events if e.get("type") == "session.start"), None)
    session_id = session_start.get("data", {}).get("sessionId") if session_start else file_path.parent.name
    trace_id = _stable_hex_id("copilot", session_id, length=32)
    active_tools: dict[str, dict] = {}
    last_ts = 0
    for index, event in enumerate(events):
        event_type = event.get("type")
        data = event.get("data", {})
        ts_ns = _parse_timestamp_to_ns(event.get("timestamp"))
        if ts_ns:
            last_ts = max(last_ts, ts_ns)
        attrs = {
            "gen_ai.client.name": "copilot",
            "gen_ai.provider.name": "github",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
        }
        if event_type == "session.start":
            yield _make_flat_span("gen_ai.client.hook.SessionStart", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "SessionStart",
            }, trace_id, f"{index}:session.start")
        elif event_type == "user.message":
            yield _make_flat_span("gen_ai.client.hook.UserPromptSubmit", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.prompt": data.get("content", ""),
            }, trace_id, f"{index}:user.message")
        elif event_type == "assistant.message":
            model = ""
            for request in data.get("toolRequests") or []:
                if isinstance(request, dict) and request.get("name"):
                    model = model or data.get("model", "")
            span_attrs = {
                **attrs,
                "gen_ai.client.hook.event": "Stop",
            }
            if model:
                span_attrs["gen_ai.request.model"] = model
            yield _make_flat_span("gen_ai.client.hook.Stop", ts_ns, ts_ns, span_attrs, trace_id, f"{index}:assistant.message")
        elif event_type == "session.shutdown":
            # Authoritative session-level token totals across all models
            metrics = data.get("modelMetrics") or {}
            total_in = sum(int((m.get("usage") or {}).get("inputTokens") or 0) for m in metrics.values())
            total_out = sum(int((m.get("usage") or {}).get("outputTokens") or 0) for m in metrics.values())
            total_cr = sum(int((m.get("usage") or {}).get("cacheReadTokens") or 0) for m in metrics.values())
            total_cw = sum(int((m.get("usage") or {}).get("cacheWriteTokens") or 0) for m in metrics.values())
            if total_in or total_out or total_cr or total_cw:
                yield _make_flat_span("gen_ai.client.hook.SessionEnd", ts_ns, ts_ns, {
                    **attrs,
                    "gen_ai.client.hook.event": "SessionEnd",
                    "gen_ai.usage.input_tokens": total_in,
                    "gen_ai.usage.output_tokens": total_out,
                    "gen_ai.usage.cache_read.input_tokens": total_cr,
                    "gen_ai.usage.cache_creation.input_tokens": total_cw,
                }, trace_id, f"{index}:session.shutdown")
        elif event_type == "tool.execution_start":
            tool_call_id = data.get("toolCallId") or f"{index}"
            active_tools[tool_call_id] = {"start_ns": ts_ns, "tool_name": data.get("toolName", "")}
            yield _make_flat_span("gen_ai.client.hook.PreToolUse", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "PreToolUse",
                "gen_ai.client.tool_name": data.get("toolName", ""),
                "gen_ai.client.tool.input": _json_dumps(data.get("arguments", {})),
            }, trace_id, f"{index}:tool.execution_start")
        elif event_type == "tool.execution_complete":
            tool_call_id = data.get("toolCallId") or f"{index}"
            start_info = active_tools.get(tool_call_id, {})
            success = bool(data.get("success", False))
            span_attrs = {
                **attrs,
                "gen_ai.client.hook.event": "PostToolUse" if success else "PostToolUseFailure",
                "gen_ai.client.tool_name": start_info.get("tool_name", ""),
            }
            if data.get("model"):
                span_attrs["gen_ai.request.model"] = data["model"]
            yield _make_flat_span(
                f"gen_ai.client.hook.{span_attrs['gen_ai.client.hook.event']}",
                start_info.get("start_ns", ts_ns),
                ts_ns,
                span_attrs,
                trace_id,
                f"{index}:tool.execution_complete",
            )
        elif event_type == "session.end":
            yield _make_flat_span("gen_ai.client.hook.SessionEnd", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "SessionEnd",
            }, trace_id, f"{index}:session.end")
    if last_ts:
        yield _make_flat_span("gen_ai.client.hook.SessionEnd", last_ts, last_ts, {
            "gen_ai.client.name": "copilot",
            "gen_ai.provider.name": "github",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "gen_ai.client.hook.event": "SessionEnd",
        }, trace_id, "synthetic:session.end")


def _iter_claude_session_spans(file_path: Path) -> Iterable[dict]:
    events = list(_load_json_lines(file_path))
    if not events:
        return
    session_id = ""
    first_ts = 0
    last_ts = 0
    trace_id = _stable_hex_id("claude", str(file_path), length=32)
    for index, event in enumerate(events):
        event_type = event.get("type")
        if event_type == "file-history-snapshot":
            continue
        session_id = session_id or event.get("sessionId", file_path.stem)
        ts_ns = _parse_timestamp_to_ns(event.get("timestamp"))
        if ts_ns:
            first_ts = ts_ns if not first_ts else min(first_ts, ts_ns)
            last_ts = max(last_ts, ts_ns)
        attrs = {
            "gen_ai.client.name": "claude",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
        }
        if event.get("cwd"):
            attrs["code.workspace.root"] = event["cwd"]
        if event_type == "user":
            yield _make_flat_span("gen_ai.client.hook.UserPromptSubmit", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.prompt": _flatten_text_content(event.get("message", {}).get("content")),
            }, trace_id, f"{index}:user")
        elif event_type == "assistant":
            message = event.get("message", {}) or {}
            usage = message.get("usage", {}) or {}
            model = message.get("model", "")
            content = message.get("content") or []
            yield _make_flat_span("gen_ai.client.hook.Stop", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.request.model": model,
                "gen_ai.usage.input_tokens": int(usage.get("input_tokens", 0) or 0),
                "gen_ai.usage.output_tokens": int(usage.get("output_tokens", 0) or 0),
                "gen_ai.usage.cache_creation.input_tokens": int(usage.get("cache_creation_input_tokens", 0) or 0),
                "gen_ai.usage.cache_read.input_tokens": int(usage.get("cache_read_input_tokens", 0) or 0),
            }, trace_id, f"{index}:assistant")
            for tool_idx, item in enumerate(content):
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                tool_name = item.get("name", "")
                tool_attrs = {
                    **attrs,
                    "gen_ai.client.hook.event": "PreToolUse",
                    "gen_ai.client.tool_name": tool_name,
                    "gen_ai.client.tool.input": _json_dumps(item.get("input", {})),
                }
                yield _make_flat_span("gen_ai.client.hook.PreToolUse", ts_ns, ts_ns, tool_attrs, trace_id, f"{index}:tool_use:{tool_idx}")
        elif event_type == "summary":
            continue
    if first_ts:
        yield _make_flat_span("gen_ai.client.hook.SessionStart", first_ts, first_ts, {
            "gen_ai.client.name": "claude",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.client.session_id": session_id or file_path.stem,
            "session.id": session_id or file_path.stem,
            "gen_ai.client.hook.event": "SessionStart",
        }, trace_id, "synthetic:session.start")
    if last_ts:
        yield _make_flat_span("gen_ai.client.hook.SessionEnd", last_ts, last_ts, {
            "gen_ai.client.name": "claude",
            "gen_ai.provider.name": "anthropic",
            "gen_ai.client.session_id": session_id or file_path.stem,
            "session.id": session_id or file_path.stem,
            "gen_ai.client.hook.event": "SessionEnd",
        }, trace_id, "synthetic:session.end")


def _iter_cursor_session_spans(file_path: Path) -> Iterable[dict]:
    events = list(_load_json_lines(file_path))
    if not events:
        return
    session_id = file_path.stem
    trace_id = _stable_hex_id("cursor", session_id, length=32)
    first_ts = 0
    last_ts = 0
    for index, event in enumerate(events):
        role = event.get("role")
        if role not in ("user", "assistant"):
            continue
        ts_ns = _parse_timestamp_to_ns(event.get("timestamp"))
        if not ts_ns:
            ts_ns = index + 1
        first_ts = ts_ns if not first_ts else min(first_ts, ts_ns)
        last_ts = max(last_ts, ts_ns)
        attrs = {
            "gen_ai.client.name": "cursor",
            "gen_ai.provider.name": "cursor",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
        }
        text = _flatten_text_content(event.get("message", {}).get("content"))
        if role == "user":
            yield _make_flat_span("gen_ai.client.hook.UserPromptSubmit", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.prompt": text,
            }, trace_id, f"{index}:user")
        elif role == "assistant":
            content = event.get("message", {}).get("content")
            yield _make_flat_span("gen_ai.client.hook.Stop", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.client.output": text,
            }, trace_id, f"{index}:assistant")
            if not isinstance(content, list):
                continue
            for tool_idx, item in enumerate(content):
                if not isinstance(item, dict) or item.get("type") != "tool_use":
                    continue
                tool_name = str(item.get("name") or "")
                tool_attrs = _cursor_tool_attrs(tool_name, item.get("input"))
                yield _make_flat_span(
                    "gen_ai.client.hook.PreToolUse",
                    ts_ns,
                    ts_ns,
                    {
                        **attrs,
                        **tool_attrs,
                        "gen_ai.client.hook.event": "PreToolUse",
                    },
                    trace_id,
                    f"{index}:tool_use:{tool_idx}",
                )
    if first_ts:
        yield _make_flat_span("gen_ai.client.hook.SessionStart", first_ts, first_ts, {
            "gen_ai.client.name": "cursor",
            "gen_ai.provider.name": "cursor",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "gen_ai.client.hook.event": "SessionStart",
        }, trace_id, "synthetic:session.start")
    if last_ts:
        yield _make_flat_span("gen_ai.client.hook.SessionEnd", last_ts, last_ts, {
            "gen_ai.client.name": "cursor",
            "gen_ai.provider.name": "cursor",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "gen_ai.client.hook.event": "SessionEnd",
        }, trace_id, "synthetic:session.end")


def _iter_gemini_session_spans(file_path: Path) -> Iterable[dict]:
    try:
        payload = _json_loads(file_path.read_text())
    except Exception as exc:
        logger.warning("Failed to read Gemini session file %s: %s", file_path, exc)
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("sessionId", file_path.stem)
    trace_id = _stable_hex_id("gemini", session_id, length=32)
    start_ns = _parse_timestamp_to_ns(payload.get("startTime"))
    last_ns = _parse_timestamp_to_ns(payload.get("lastUpdated"))
    if start_ns:
        yield _make_flat_span("gen_ai.client.hook.SessionStart", start_ns, start_ns, {
            "gen_ai.client.name": "gemini",
            "gen_ai.provider.name": "google",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "gen_ai.client.hook.event": "SessionStart",
        }, trace_id, "synthetic:session.start")
    for index, message in enumerate(payload.get("messages") or []):
        if not isinstance(message, dict):
            continue
        ts_ns = _parse_timestamp_to_ns(message.get("timestamp"))
        last_ns = max(last_ns, ts_ns)
        attrs = {
            "gen_ai.client.name": "gemini",
            "gen_ai.provider.name": "google",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
        }
        if message.get("type") == "user":
            yield _make_flat_span("gen_ai.client.hook.UserPromptSubmit", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "UserPromptSubmit",
                "gen_ai.client.prompt": message.get("content", ""),
            }, trace_id, f"{index}:user")
        elif message.get("type") == "gemini":
            tokens = message.get("tokens") or {}
            span_attrs = {
                **attrs,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.request.model": message.get("model", ""),
                "gen_ai.usage.input_tokens": int(tokens.get("input", 0) or 0),
                "gen_ai.usage.output_tokens": int(tokens.get("output", 0) or 0),
            }
            yield _make_flat_span("gen_ai.client.hook.Stop", ts_ns, ts_ns, span_attrs, trace_id, f"{index}:gemini")
            for tool_idx, call in enumerate(message.get("toolCalls") or []):
                if not isinstance(call, dict):
                    continue
                tool_ts = _parse_timestamp_to_ns(call.get("timestamp")) or ts_ns
                tool_name = call.get("displayName") or call.get("name", "")
                yield _make_flat_span("gen_ai.client.hook.PreToolUse", tool_ts, tool_ts, {
                    **attrs,
                    "gen_ai.client.hook.event": "PreToolUse",
                    "gen_ai.client.tool_name": tool_name,
                    "gen_ai.client.tool.input": _json_dumps(call.get("args", {})),
                }, trace_id, f"{index}:tool:{tool_idx}:start")
                yield _make_flat_span(
                    f"gen_ai.client.hook.{'PostToolUse' if call.get('status') == 'success' else 'PostToolUseFailure'}",
                    tool_ts,
                    tool_ts,
                    {
                        **attrs,
                        "gen_ai.client.hook.event": "PostToolUse" if call.get("status") == "success" else "PostToolUseFailure",
                        "gen_ai.client.tool_name": tool_name,
                    },
                    trace_id,
                    f"{index}:tool:{tool_idx}:end",
                )
    if last_ns:
        yield _make_flat_span("gen_ai.client.hook.SessionEnd", last_ns, last_ns, {
            "gen_ai.client.name": "gemini",
            "gen_ai.provider.name": "google",
            "gen_ai.client.session_id": session_id,
            "session.id": session_id,
            "gen_ai.client.hook.event": "SessionEnd",
        }, trace_id, "synthetic:session.end")


def _load_rich_session_spans() -> tuple[list[dict], dict[str, int], dict[str, tuple[str, str]]]:
    """Load spans from native session stores.

    Returns (flat_spans, event_counts_by_file, session_source_map).
    session_source_map: {session_id: (agent_name, file_path_str)}
    """
    spans: list[dict] = []
    counts: dict[str, int] = {}
    source_map: dict[str, tuple[str, str]] = {}
    for source, file_path in _discover_rich_session_files():
        if source == "codex":
            derived = list(_iter_codex_session_spans(file_path))
        elif source == "copilot":
            derived = list(_iter_copilot_session_spans(file_path))
        elif source == "cursor":
            derived = list(_iter_cursor_session_spans(file_path))
        elif source == "claude":
            derived = list(_iter_claude_session_spans(file_path))
        elif source == "gemini":
            derived = list(_iter_gemini_session_spans(file_path))
        else:
            derived = []
        if derived:
            key = f"{source}:{file_path.name}"
            counts[key] = len(derived)
            spans.extend(derived)
            # Track which file each session came from
            for sp in derived:
                sid = _extract_session_id(sp.get("attributes") or {})
                if sid and sid not in source_map:
                    source_map[sid] = (source, str(file_path))
    return spans, counts, source_map


def _otlp_attr_value(value) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


def _encode_otlp_span(span: dict) -> dict:
    attrs = span.get("attributes") or {}
    return {
        "traceId": span.get("traceId", ""),
        "spanId": span.get("spanId", ""),
        "parentSpanId": span.get("parentSpanId", ""),
        "name": span.get("name", ""),
        "startTimeUnixNano": str(int(span.get("start_time_ns", 0) or 0)),
        "endTimeUnixNano": str(int(span.get("end_time_ns", 0) or 0)),
        "attributes": [
            {"key": key, "value": _otlp_attr_value(value)}
            for key, value in sorted(attrs.items())
        ],
    }


def _canonical_otlp_traces_path() -> Path:
    return REFLECT_HOME / "state" / "otlp" / "otel-traces.json"


def _materialize_local_otlp_traces(
    sessions_dir: Path,
    spans_dir: Path,
    *,
    force_from_sessions: bool = False,
) -> Path | None:
    flat_spans: list[dict] = []
    if not force_from_sessions and spans_dir.exists():
        for span_file in sorted(spans_dir.glob("*.jsonl")):
            flat_spans.extend(_load_json_lines(span_file))

    if not flat_spans and sessions_dir == _default_sessions_dir():
        rich_spans, _, _ = _load_rich_session_spans()
        flat_spans.extend(rich_spans)

    if not flat_spans:
        return None

    out = _canonical_otlp_traces_path()
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resourceSpans": [
            {
                "resource": {"attributes": []},
                "scopeSpans": [
                    {
                        "scope": {"name": "reflect.local-cache"},
                        "spans": [_encode_otlp_span(span) for span in flat_spans],
                    }
                ],
            }
        ]
    }
    out.write_text(_json_stdlib.dumps(payload) + "\n", encoding="utf-8")
    return out


def _extract_event(span: dict) -> str | None:
    """Extract event type from OTLP span (attributes) or raw event format."""
    attrs = span.get("attributes") or {}
    # OTLP format (current hook output)
    event = attrs.get("gen_ai.client.hook.event") or attrs.get("ide.hook.event")
    if event:
        return event
    # Span name fallback: ide.hook.EventName
    name = span.get("name", "")
    if name.startswith("gen_ai.client.hook."):
        return name[len("gen_ai.client.hook."):]
    if name.startswith("ide.hook."):
        return name[len("ide.hook."):]
    # Legacy raw format
    raw = span.get("event")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _shorten_mcp_server(server: str) -> str:
    normalized = str(server or "").strip()
    if not normalized:
        return ""

    lower = normalized.lower()
    if re.fullmatch(r"[a-z0-9._:-]{1,64}", lower) and " " not in lower and "/" not in lower:
        return normalized

    mcp_name_match = re.search(r"\b(mcp-[a-z0-9][a-z0-9._-]*)\b", lower)
    if mcp_name_match and mcp_name_match.group(1) != "mcp-remote":
        return mcp_name_match.group(1)

    url_match = re.search(r"https?://[^\s]+", normalized)
    if url_match:
        parsed = urlparse(url_match.group(0))
        host = (parsed.netloc or parsed.path).lower().split("@")[-1].split(":")[0]
        if "coralogix.us" in host:
            return "mcp-coralogix-us"
        if "coralogix.com" in host:
            return "mcp-coralogix"
        if host:
            return f"mcp-{host.split('.')[0]}"

    server_name_match = re.search(r"\bserver-([a-z0-9][a-z0-9._-]*)\b", lower)
    if server_name_match:
        return f"mcp-{server_name_match.group(1)}"

    keyword_map = {
        "atlassian": "mcp-atlassian",
        "jira": "mcp-atlassian",
        "confluence": "mcp-atlassian",
        "gitlab": "mcp-gitlab",
        "postgres": "mcp-postgres",
        "coralogix": "mcp-coralogix",
        "playwright": "mcp-playwright",
        "cloudflare": "mcp-cloudflare",
        "wiz": "mcp-wiz",
    }
    for keyword, short_name in keyword_map.items():
        if keyword in lower:
            return short_name

    if len(normalized) > 60 and "/" in normalized:
        tail = normalized.split("/")[-1].split(":")[0].strip()
        if tail:
            return tail

    return "mcp-command"


def _extract_session_id(attrs: dict) -> str:
    return (
        attrs.get("gen_ai.client.session_id")
        or attrs.get("session.id")
        or attrs.get("ide.session_id")
        or ""
    )


def _extract_model_name(attrs: dict) -> str:
    return (
        attrs.get("gen_ai.response.model")
        or attrs.get("gen_ai.request.model")
        or attrs.get("model")
        or ""
    )


def _infer_otlp_logs_file(otlp_traces_file: Path | None) -> Path | None:
    if otlp_traces_file:
        sibling = otlp_traces_file.with_name("otel-logs.json")
        if sibling.exists():
            return sibling
    default_logs = REFLECT_HOME / "state" / "otel-logs.json"
    return default_logs if default_logs.exists() else None


def _load_session_model_hints(session_files: list[Path]) -> dict[str, str]:
    hints: dict[str, str] = {}
    for session_file in session_files:
        try:
            payload = _json_loads(session_file.read_text())
        except Exception as exc:
            logger.warning("Failed to read session model hints from %s: %s", session_file, exc)
            continue
        if not isinstance(payload, dict):
            continue
        model = payload.get("last_known_model")
        if isinstance(model, str) and model.strip():
            hints[session_file.stem] = model.strip()
    return hints


def _enrich_missing_session_models_from_logs(
    otlp_logs_file: Path | None,
    sessions_seen: set[str],
    session_models: dict[str, Counter],
) -> None:
    """Fill blank per-session models from OTLP logs without changing main event counts."""
    if otlp_logs_file is None or not otlp_logs_file.exists():
        return

    missing_sessions = {sid for sid in sessions_seen if not session_models.get(sid)}
    if not missing_sessions:
        return

    log_model_counts: dict[str, Counter] = {}
    log_model_latest_ts: dict[str, dict[str, int]] = {}
    for record in _load_otlp_logs(otlp_logs_file):
        attrs = record.get("attributes") or {}
        session_id = _extract_session_id(attrs)
        model = _extract_model_name(attrs)
        if not session_id or not model or session_id not in missing_sessions:
            continue
        log_model_counts.setdefault(session_id, Counter())[model] += 1
        ts_ns = int(record.get("time_ns", 0) or 0)
        log_model_latest_ts.setdefault(session_id, {})[model] = max(
            log_model_latest_ts.setdefault(session_id, {}).get(model, 0),
            ts_ns,
        )

    for session_id, model_counts in log_model_counts.items():
        if model_counts:
            top_count = max(model_counts.values())
            tied_models = [model for model, count in model_counts.items() if count == top_count]
            if len(tied_models) > 1:
                preferred = max(
                    tied_models,
                    key=lambda model: log_model_latest_ts.get(session_id, {}).get(model, 0),
                )
                model_counts[preferred] += 1
            session_models[session_id] = model_counts


def _default_sessions_dir() -> Path:
    """Lazy import to avoid circular dependency with core."""
    from reflect.core import _default_sessions_dir as _core_default_sessions_dir
    return _core_default_sessions_dir()
