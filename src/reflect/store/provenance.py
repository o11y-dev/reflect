from __future__ import annotations

from typing import Any

from reflect.agent_capabilities import AGENT_CAPABILITIES, normalize_agent_key

ORIGIN_NATIVE_OTLP_LOG = "native_otlp_log"
ORIGIN_NATIVE_OTLP_TRACE = "native_otlp_trace"
ORIGIN_HOOK_OTLP_LOG = "hook_otlp_log"
ORIGIN_HOOK_OTLP_TRACE = "hook_otlp_trace"
ORIGIN_HOOK_JSONL = "hook_jsonl"
ORIGIN_NATIVE_SESSION = "native_session"

KNOWN_ORIGIN_KINDS = {
    ORIGIN_NATIVE_OTLP_LOG,
    ORIGIN_NATIVE_OTLP_TRACE,
    ORIGIN_HOOK_OTLP_LOG,
    ORIGIN_HOOK_OTLP_TRACE,
    ORIGIN_HOOK_JSONL,
    ORIGIN_NATIVE_SESSION,
}

NATIVE_OTLP_ORIGINS = {ORIGIN_NATIVE_OTLP_LOG, ORIGIN_NATIVE_OTLP_TRACE}
HOOK_OTLP_ORIGINS = {ORIGIN_HOOK_OTLP_LOG, ORIGIN_HOOK_OTLP_TRACE}
HOOK_ORIGINS = HOOK_OTLP_ORIGINS | {ORIGIN_HOOK_JSONL}

CODEX_OTLP_SERVICES = frozenset(
    service.lower()
    for agent in AGENT_CAPABILITIES if agent.key == "codex"
    for service in agent.otel_service_names
)
_NATIVE_LOG_SERVICES = {"claude-code", "gemini-cli"} | set(CODEX_OTLP_SERVICES)

_ORIGIN_LABELS = {
    ORIGIN_NATIVE_OTLP_LOG: "Native OTLP logs",
    ORIGIN_NATIVE_OTLP_TRACE: "Native OTLP traces",
    ORIGIN_HOOK_OTLP_LOG: "Hook OTLP logs",
    ORIGIN_HOOK_OTLP_TRACE: "Hook OTLP traces",
    ORIGIN_HOOK_JSONL: "Hook local spans",
    ORIGIN_NATIVE_SESSION: "Native sessions",
}

_ORIGIN_TRANSPORTS = {
    ORIGIN_NATIVE_OTLP_LOG: "native_otlp",
    ORIGIN_NATIVE_OTLP_TRACE: "native_otlp",
    ORIGIN_HOOK_OTLP_LOG: "hook_otlp",
    ORIGIN_HOOK_OTLP_TRACE: "hook_otlp",
    ORIGIN_HOOK_JSONL: "hook_local",
    ORIGIN_NATIVE_SESSION: "native_session",
}


def normalize_origin_kind(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned if cleaned in KNOWN_ORIGIN_KINDS else None


def is_codex_otel_service(value: object) -> bool:
    return str(value or "").strip().lower() in CODEX_OTLP_SERVICES


def telemetry_agent_name(attrs: dict[str, Any]) -> str:
    """Map Codex transport identities to the product, without rewriting raw attributes."""
    name = str(
        attrs.get("gen_ai.client.name") or attrs.get("ide.name")
        or attrs.get("agent.name") or attrs.get("service.name") or "unknown"
    )
    return "codex" if normalize_agent_key(name) == "codex" else name


def telemetry_session_id(attrs: dict[str, Any]) -> str | None:
    for key in (
        "session.id", "gen_ai.session.id", "gen_ai.client.session_id",
        "gen_ai.conversation.id", "conversation.id", "session_id", "ide.session_id",
    ):
        if value := str(attrs.get(key) or "").strip():
            return value
    return None


def unassigned_codex_classification(attrs: dict[str, Any]) -> str | None:
    """Keep unlinked Codex evidence out of file-derived synthetic work sessions."""
    if telemetry_session_id(attrs):
        return None
    if telemetry_agent_name(attrs) != "codex":
        return None
    # A sessionless hook or semantic record is evidence we cannot yet attribute,
    # not proof of a conversation or permission to attach it to a nearby one.
    semantic_keys = (
        "gen_ai.client.hook.event", "ide.hook.event", "event.name",
        "gen_ai.operation.name", "gen_ai.client.tool_name", "gen_ai.client.mcp_tool",
        "gen_ai.request.model", "gen_ai.response.model", "gen_ai.client.prompt",
    )
    if any(attrs.get(key) for key in semantic_keys) or any(
        key.startswith("gen_ai.usage.") for key in attrs
    ):
        return "unattributed_session"
    return "runtime_internal" if is_codex_otel_service(attrs.get("service.name")) else "unattributed_session"


def has_hook_semantics(attrs: dict[str, Any]) -> bool:
    return any(
        isinstance(attrs.get(key), str) and attrs.get(key)
        for key in ("gen_ai.client.hook.event", "ide.hook.event")
    )


def classify_origin_kind(source_type: str, attrs: dict[str, Any]) -> str | None:
    explicit = normalize_origin_kind(attrs.get("reflect.telemetry.origin"))
    if explicit:
        return explicit

    if source_type == "native_session":
        return ORIGIN_NATIVE_SESSION
    if source_type == "local_spans_jsonl":
        return ORIGIN_HOOK_JSONL
    if source_type == "otlp_logs_json":
        service = str(attrs.get("service.name") or "").strip().lower()
        if service in _NATIVE_LOG_SERVICES:
            return ORIGIN_NATIVE_OTLP_LOG
        if has_hook_semantics(attrs):
            return ORIGIN_HOOK_OTLP_LOG
        return ORIGIN_NATIVE_OTLP_LOG
    if source_type == "otlp_traces_json":
        if has_hook_semantics(attrs):
            return ORIGIN_HOOK_OTLP_TRACE
        return ORIGIN_NATIVE_OTLP_TRACE
    return None


def apply_origin_kind(attrs: dict[str, Any], origin_kind: str | None) -> dict[str, Any]:
    if not origin_kind:
        return dict(attrs)
    updated = dict(attrs)
    updated["reflect.telemetry.origin"] = origin_kind
    return updated


def stable_hash_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    derived_keys = {
        "reflect.telemetry.classification",
        "reflect.telemetry.origin",
    }
    if not derived_keys.intersection(attrs):
        return attrs
    sanitized = dict(attrs)
    for key in derived_keys:
        sanitized.pop(key, None)
    return sanitized


def origin_label(origin_kind: str | None) -> str:
    return _ORIGIN_LABELS.get(origin_kind or "", origin_kind or "Unknown")


def origin_transport(origin_kind: str | None) -> str:
    return _ORIGIN_TRANSPORTS.get(origin_kind or "", "unknown")
