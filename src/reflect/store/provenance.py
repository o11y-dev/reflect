from __future__ import annotations

from typing import Any

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

_NATIVE_LOG_SERVICES = {"claude-code", "codex_cli_rs", "codex-app-server", "gemini-cli"}

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
    if "reflect.telemetry.origin" not in attrs:
        return attrs
    sanitized = dict(attrs)
    sanitized.pop("reflect.telemetry.origin", None)
    return sanitized


def origin_label(origin_kind: str | None) -> str:
    return _ORIGIN_LABELS.get(origin_kind or "", origin_kind or "Unknown")


def origin_transport(origin_kind: str | None) -> str:
    return _ORIGIN_TRANSPORTS.get(origin_kind or "", "unknown")
