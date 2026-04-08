from __future__ import annotations

import hashlib
import json as _json_stdlib
import os
import re
from collections import Counter
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from reflect.utils import _json_dumps, _json_loads

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
                        yield flat_span


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


def _discover_rich_session_files() -> list[tuple[str, Path]]:
    home = Path.home()
    candidates: list[tuple[str, Path]] = []
    candidates.extend(("copilot", p) for p in sorted((home / ".copilot" / "session-state").glob("*/events.jsonl")))
    candidates.extend(("cursor", p) for p in sorted((home / ".cursor" / "projects").glob("**/agent-transcripts/**/*.jsonl")))
    candidates.extend(("claude", p) for p in sorted((home / ".claude" / "projects").glob("**/*.jsonl")))
    candidates.extend(("gemini", p) for p in sorted((home / ".gemini" / "tmp").glob("**/chats/session-*.json")))
    return candidates


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
            yield _make_flat_span("gen_ai.client.hook.Stop", ts_ns, ts_ns, {
                **attrs,
                "gen_ai.client.hook.event": "Stop",
                "gen_ai.client.output": text,
            }, trace_id, f"{index}:assistant")
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
    except Exception:
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
        if source == "copilot":
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


def _discover_rich_session_source_map() -> dict[str, tuple[str, str]]:
    """Lightweight discovery: map session_id -> (agent, filepath) without parsing spans.

    Extracts session IDs from filenames/paths rather than parsing file contents,
    so this is fast even with hundreds of session files.
    """
    source_map: dict[str, tuple[str, str]] = {}
    for source, file_path in _discover_rich_session_files():
        if source == "copilot":
            sid = file_path.parent.name  # copilot: .copilot/session-state/<sid>/events.jsonl
        elif source == "gemini":
            # gemini: session-<uuid>.json
            stem = file_path.stem
            sid = stem.replace("session-", "") if stem.startswith("session-") else stem
        else:
            # claude/cursor: <session-id>.jsonl
            sid = file_path.stem
        if sid and sid not in source_map:
            source_map[sid] = (source, str(file_path))
    return source_map


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
    event = attrs.get("gen_ai.client.hook.event")
    if event:
        return event
    # Span name fallback: ide.hook.EventName
    name = span.get("name", "")
    if name.startswith("gen_ai.client.hook."):
        return name[len("gen_ai.client.hook."):]
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
        except Exception:
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
