from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from reflect.store.command_patterns import refresh_command_projection
from reflect.store.hook_facts import HookFactRepository, backfill_hook_facts
from reflect.store.mcp import DEFAULT_MCP_CLASSIFIER, MCPCallBackfill
from reflect.store.pending_refresh import PendingRefreshState
from reflect.store.provenance import (
    apply_origin_kind,
    classify_origin_kind,
    is_codex_otel_service,
    telemetry_agent_name,
    telemetry_session_id,
    unassigned_codex_classification,
)
from reflect.store.workspaces import backfill_session_context
from reflect.task_runs import TaskRunReconciler
from reflect.telemetry_facts import TOOL_NAME_KEYS, recorded_tool_call_id, recorded_tool_name
from reflect.tool_outcomes import with_tool_outcome
from reflect.usage_facts import usage_facts
from reflect.utils import _json_dumps

NON_OPERATION_SPAN_TYPES = frozenset({"runtime_span", "session_span", "generation_span"})


def _now() -> str:
    return datetime.now(tz=UTC).isoformat()


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    digest = hashlib.sha1(payload.encode()).hexdigest()
    return f"{prefix}_{digest}"


def _load_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _to_int(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _valid_source_timestamp(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.year >= 2000


def _duration_ms(observed_at: str, received_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(observed_at)
        end = datetime.fromisoformat(received_at)
    except ValueError:
        return None
    delta = int((end - start).total_seconds() * 1000)
    return max(delta, 0)


def _first_text(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _logical_tool_call_id(attrs: dict[str, Any], fallback: object) -> str:
    """Return the provider-neutral identity shared by invocation and result events."""
    if _is_codex_dispatch(attrs):
        return str(attrs["call_id"]).strip()
    return (
        DEFAULT_MCP_CLASSIFIER.call_id(attrs)
        or recorded_tool_call_id(attrs)
        or str(fallback)
    )


def _is_codex_dispatch(attrs: dict[str, Any]) -> bool:
    call_id = attrs.get("call_id")
    return bool(
        attrs.get("reflect.telemetry.origin") == "native_otlp_trace"
        and is_codex_otel_service(attrs.get("service.name"))
        and attrs.get("code.module.name") == "codex_core::tools::parallel"
        and attrs.get("codex.tool.phase") == "dispatch"
        and telemetry_session_id(attrs)
        and _first_text(attrs, "turn.id", "turn_id")
        and isinstance(call_id, str)
        and call_id.strip()
        and recorded_tool_name(attrs)
    )


def _canonical_tool_name(attrs: dict[str, Any], fallback: object) -> str:
    raw_name = str(
        recorded_tool_name(attrs)
        or ("Shell" if attrs.get("gen_ai.client.command") else "")
        or fallback
    )
    identity = DEFAULT_MCP_CLASSIFIER.identify(attrs)
    if identity.server_name and identity.tool_name:
        return f"mcp__{identity.server_name}__{identity.tool_name}"
    return raw_name


def _first_hash(attrs: dict[str, Any], text: str | None, *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None


def backfill_tool_call_hashes(conn: sqlite3.Connection) -> dict[str, int]:
    """Fill privacy-safe fingerprints from already-redacted tool previews.

    Older canonical rows predate tool input/output fingerprint persistence. The
    preview is already the local, redacted value used by the dashboard, so this
    repair stores only its SHA-256 digest and never introduces new raw content.
    """
    rows = conn.execute(
        """
        SELECT id, input_preview_redacted, output_preview_redacted
        FROM tool_calls
        WHERE (NULLIF(input_hash, '') IS NULL AND NULLIF(input_preview_redacted, '') IS NOT NULL)
           OR (NULLIF(output_hash, '') IS NULL AND NULLIF(output_preview_redacted, '') IS NOT NULL)
        """
    ).fetchall()
    updated = 0
    for tool_call_id, input_preview, output_preview in rows:
        input_hash = (
            hashlib.sha256(str(input_preview).encode("utf-8")).hexdigest()
            if input_preview not in (None, "")
            else None
        )
        output_hash = (
            hashlib.sha256(str(output_preview).encode("utf-8")).hexdigest()
            if output_preview not in (None, "")
            else None
        )
        cursor = conn.execute(
            """
            UPDATE tool_calls
            SET input_hash = COALESCE(NULLIF(input_hash, ''), ?),
                output_hash = COALESCE(NULLIF(output_hash, ''), ?),
                updated_at = CASE
                  WHEN (NULLIF(input_hash, '') IS NULL AND ? IS NOT NULL)
                    OR (NULLIF(output_hash, '') IS NULL AND ? IS NOT NULL)
                  THEN ? ELSE updated_at END
            WHERE id = ?
            """,
            (input_hash, output_hash, input_hash, output_hash, _now(), tool_call_id),
        )
        updated += max(0, cursor.rowcount)
    return {"updated": updated}


def backfill_mcp_calls(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
    changed_session_ids: set[str] | None = None,
) -> dict[str, int]:
    return MCPCallBackfill(conn).run(
        session_ids=session_ids,
        changed_session_ids=changed_session_ids,
    )


_TOKEN_ATTRIBUTE_KEYS = (
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.cache_creation.input_tokens",
    "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.reasoning_output_tokens",
)


def _has_token_attributes(attrs: dict[str, Any]) -> bool:
    return any(key in attrs for key in _TOKEN_ATTRIBUTE_KEYS)


def _canonical_usage_attrs(attrs: dict[str, Any]) -> dict[str, Any]:
    """Normalize known source bucket semantics without turning missing into zero."""
    result = dict(attrs)
    for target, aliases in {
        "gen_ai.usage.cache_read.input_tokens": ("gen_ai.usage.cache_read_input_tokens", "hook.cache_read_tokens"),
        "gen_ai.usage.cache_creation.input_tokens": (
            "gen_ai.usage.cache_write.input_tokens",
            "gen_ai.usage.cache_creation_input_tokens",
            "hook.cache_write_tokens",
        ),
    }.items():
        if target not in result:
            for alias in aliases:
                if result.get(alias) is not None:
                    result[target] = result[alias]
                    break
    inclusive = result.get("reflect.usage.input_semantics") == "inclusive" or (
        telemetry_agent_name(result) == "cursor"
        and result.get("reflect.usage.input_semantics") != "uncached"
    )
    if inclusive and result.get("gen_ai.usage.input_tokens") is not None:
        value = result["gen_ai.usage.input_tokens"]
        result["reflect.usage.inclusive_input_tokens"] = value
        keys = ("gen_ai.usage.cache_read.input_tokens", "gen_ai.usage.cache_creation.input_tokens")
        if all(result.get(key) is not None for key in keys):
            try:
                buckets = [int(value), *(int(result[key]) for key in keys)]
            except (TypeError, ValueError, OverflowError):
                buckets = [-1]
            uncached = buckets[0] - sum(buckets[1:])
            if all(bucket >= 0 for bucket in buckets) and uncached >= 0:
                result["gen_ai.usage.input_tokens"] = uncached
                result["reflect.usage.input_semantics"] = "uncached"
                return result
        result.pop("gen_ai.usage.input_tokens")
        result["reflect.usage.input_semantics"] = "unavailable"
        result["reflect.usage.unavailable_reason"] = "missing_or_inconsistent_cache_buckets"
    return result


def _has_token_usage(attrs: dict[str, Any]) -> bool:
    return any(_to_int(attrs.get(key)) > 0 for key in _TOKEN_ATTRIBUTE_KEYS)


def _has_model_exchange_evidence(attrs: dict[str, Any]) -> bool:
    return bool(
        attrs.get("gen_ai.operation.name")
        or attrs.get("gen_ai.request.model")
        or attrs.get("gen_ai.response.model")
        or _has_token_attributes(attrs)
    )


def _step_type(event_type: str, attrs: dict[str, Any]) -> str:
    event = _event_name(event_type, attrs).lower()
    if attrs.get("gen_ai.memory.id"):
        return "memory_event"
    if DEFAULT_MCP_CLASSIFIER.is_explicit_event(event_type, attrs):
        return "mcp_call"
    if "subagent" in event or attrs.get("gen_ai.client.agent_id"):
        return "agent_call"
    if attrs.get("gen_ai.client.command"):
        return "shell_command"
    # Runtime spans can mention tools while only measuring queue/wait time.
    # A tool projection needs a recorded identity or explicit invocation event.
    if (
        recorded_tool_name(attrs)
        or event in {"pretooluse", "posttooluse", "posttoolusefailure", "beforetoolcall", "aftertoolcall"}
        or str(attrs.get("event.name") or event_type).lower() in {"tool.execution_start", "tool.execution_complete"}
        or attrs.get("gen_ai.operation.name") == "execute_tool"
    ):
        return "tool_call"
    if event in {"userpromptsubmit", "user.message", "user_message"}:
        return "llm_call" if _has_token_usage(attrs) else "conversation_event"
    if event in {"sessionstart", "sessionend", "session.start", "session.end"}:
        return "llm_call" if _has_token_usage(attrs) else "lifecycle"
    if event in {"assistantmessage", "usage"} and not _has_token_attributes(attrs):
        return "conversation_event"
    if event == "stop" and not _has_model_exchange_evidence(attrs):
        return "conversation_event"
    if (
        _has_model_exchange_evidence(attrs)
        or attrs.get("gen_ai.client.prompt.sha256")
        or attrs.get("gen_ai.client.response.sha256")
        or attrs.get("gen_ai.client.stop_message.sha256")
        or "llm" in event
    ):
        return "llm_call"
    if "error" in event or "fail" in event:
        return "error"
    # Container and runtime instrumentation is retained evidence, but does not
    # establish an invocation, a model exchange, or a task boundary. Explicit
    # operation semantics above always take precedence over this fallback.
    full_event = str(attrs.get("gen_ai.client.hook.event") or attrs.get("ide.hook.event")
                     or attrs.get("event.name") or event_type).lower()
    if (_first_text(attrs, "gen_ai.client.session_id") or "").strip():
        if full_event == "gen_ai.client.session":
            return "session_span"
        if full_event == "gen_ai.client.generation" and (_first_text(attrs, "gen_ai.client.generation_id") or "").strip():
            return "generation_span"
    if (
        (_first_text(attrs, "code.module.name") or "").strip()
        and all(type(attrs.get(key)) in (int, float) and 0 <= attrs[key] < float("inf")
                for key in ("busy_ns", "idle_ns"))
    ):
        return "runtime_span"
    return "unknown"


def _status(event_type: str, attrs: dict[str, Any]) -> str:
    status = str(attrs.get("gen_ai.client.status") or attrs.get("status") or "").lower()
    event = _event_name(event_type, attrs).lower()
    if status in {"ok", "error", "skipped", "unknown"}:
        return status
    if status in {"completed", "complete", "success", "succeeded"}:
        return "ok"
    if status in {"failed", "failure"}:
        return "error"
    if "error" in event or "fail" in event:
        return "error"
    if event.startswith("after") or event.startswith("post") or event in {"stop", "sessionend"}:
        return "ok"
    return "unknown"


def _event_name(event_type: str, attrs: dict[str, Any]) -> str:
    event = (
        attrs.get("gen_ai.client.hook.event")
        or attrs.get("ide.hook.event")
        or attrs.get("event.name")
        or event_type
    )
    return str(event).rsplit(".", 1)[-1]


def _next_step_seq(conn: sqlite3.Connection, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), -1) + 1 FROM steps WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return int(row[0])


def _insert_agent(conn: sqlite3.Connection, attrs: dict[str, Any], timestamp: str) -> str:
    name = telemetry_agent_name(attrs)
    agent_id = _stable_id("agent", name)
    conn.execute(
        """
        INSERT OR IGNORE INTO agents(id, name, kind, raw_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (agent_id, name, attrs.get("gen_ai.client.kind"), json.dumps(attrs, sort_keys=True), timestamp, timestamp),
    )
    return agent_id


def _upsert_session(
    conn: sqlite3.Connection,
    *,
    session_id: str,
    agent_id: str,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    timestamp: str,
) -> None:
    input_tokens = _to_int(attrs.get("gen_ai.usage.input_tokens"))
    output_tokens = _to_int(attrs.get("gen_ai.usage.output_tokens"))
    cache_creation = _to_int(attrs.get("gen_ai.usage.cache_creation.input_tokens"))
    cache_read = _to_int(attrs.get("gen_ai.usage.cache_read.input_tokens"))
    reasoning = _to_int(attrs.get("gen_ai.usage.reasoning_output_tokens"))
    token_provenance = "local_telemetry" if _has_token_attributes(attrs) else "unavailable"
    source_timestamps = [
        str(value)
        for value in (raw_event["observed_at"], raw_event["received_at"])
        if _valid_source_timestamp(str(value) if value else None)
    ]
    last_observed_at = max(source_timestamps, default=None)
    context_only = bool(attrs.get("gen_ai.memory.id") or attrs.get("reflect.usage.enrichment_only"))
    conn.execute(
        """
        INSERT INTO sessions(
          id, agent_id, started_at, ended_at, status, input_tokens, output_tokens,
          cache_creation_tokens, cache_read_tokens, reasoning_tokens, source_kind,
          source_ref, token_provenance, last_observed_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          agent_id = CASE
            WHEN excluded.source_kind = 'native_session' THEN excluded.agent_id
            ELSE COALESCE(sessions.agent_id, excluded.agent_id)
          END,
          started_at = CASE
            WHEN (
              sessions.started_at IS NULL OR sessions.started_at = ''
              OR substr(sessions.started_at, 1, 4) < '2000'
            ) AND excluded.started_at <> ''
              AND substr(excluded.started_at, 1, 4) >= '2000'
              THEN excluded.started_at
            WHEN sessions.started_at <> ''
              AND substr(sessions.started_at, 1, 4) >= '2000'
              AND excluded.started_at <> ''
              AND substr(excluded.started_at, 1, 4) >= '2000'
              THEN MIN(sessions.started_at, excluded.started_at)
            ELSE sessions.started_at
          END,
          ended_at = CASE
            WHEN sessions.ended_at IS NULL THEN excluded.ended_at
            WHEN excluded.ended_at IS NULL THEN sessions.ended_at
            ELSE MAX(sessions.ended_at, excluded.ended_at)
          END,
          last_observed_at = CASE
            WHEN sessions.last_observed_at IS NULL THEN excluded.last_observed_at
            WHEN excluded.last_observed_at IS NULL THEN sessions.last_observed_at
            ELSE MAX(sessions.last_observed_at, excluded.last_observed_at)
          END,
          input_tokens = sessions.input_tokens + excluded.input_tokens,
          output_tokens = sessions.output_tokens + excluded.output_tokens,
          cache_creation_tokens = sessions.cache_creation_tokens + excluded.cache_creation_tokens,
          cache_read_tokens = sessions.cache_read_tokens + excluded.cache_read_tokens,
          reasoning_tokens = sessions.reasoning_tokens + excluded.reasoning_tokens,
          token_provenance = CASE
            WHEN excluded.token_provenance = 'local_telemetry'
              THEN excluded.token_provenance
            WHEN sessions.token_provenance = 'unavailable'
              THEN excluded.token_provenance
            ELSE sessions.token_provenance
          END,
          status = CASE
            WHEN sessions.status = 'error' OR excluded.status = 'error' THEN 'error'
            WHEN excluded.status = 'ok' THEN 'ok'
            WHEN sessions.status IN ('ok', 'active') THEN sessions.status
            ELSE excluded.status
          END,
          source_kind = CASE
            WHEN excluded.source_kind = 'native_session' THEN excluded.source_kind
            ELSE COALESCE(sessions.source_kind, excluded.source_kind)
          END,
          source_ref = CASE
            WHEN excluded.source_kind = 'native_session' THEN excluded.source_ref
            ELSE COALESCE(sessions.source_ref, excluded.source_ref)
          END,
          updated_at = excluded.updated_at
        """,
        (
            session_id,
            agent_id,
            raw_event["observed_at"],
            raw_event["received_at"],
            _status(raw_event["event_type"], attrs),
            input_tokens,
            output_tokens,
            cache_creation,
            cache_read,
            reasoning,
            None if context_only else raw_event["source_type"],
            None if context_only else raw_event["source_id"],
            token_provenance,
            last_observed_at,
            timestamp,
            timestamp,
        ),
    )


def _insert_step(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_type: str,
    timestamp: str,
) -> str:
    step_id = _stable_id("step", raw_event["id"])
    conn.execute(
        """
        INSERT OR IGNORE INTO steps(
          id, session_id, seq, type, started_at, ended_at, duration_ms,
          status, summary, origin_kind, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_id,
            session_id,
            _next_step_seq(conn, session_id),
            step_type,
            raw_event["observed_at"],
            raw_event["received_at"],
            _duration_ms(raw_event["observed_at"], raw_event["received_at"]),
            _status(raw_event["event_type"], attrs),
            raw_event["event_type"],
            raw_event["origin_kind"],
            json.dumps(attrs, sort_keys=True),
            timestamp,
            timestamp,
        ),
    )
    return step_id


_PROVENANCE_REPAIR_KEY = "telemetry_provenance_legacy_repair_v1"


def repair_telemetry_provenance(
    conn: sqlite3.Connection, *, incremental: bool = False,
) -> dict[str, int]:
    """Repair provenance, committing the existing caller transaction on success.

    Explicit repairs inspect historical JSON. Routine normalization does this
    once, then uses the origin index; ingestion already writes both fields.
    """
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    owned_transaction = not conn.in_transaction
    savepoint_started = False
    try:
        if owned_transaction:
            conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT reflect_provenance_repair")
        savepoint_started = True
        legacy = not incremental or conn.execute(
            "SELECT 1 FROM store_metadata WHERE key = ? AND value = 'complete'",
            (_PROVENANCE_REPAIR_KEY,),
        ).fetchone() is None
        predicate = "origin_kind IS NULL"
        if legacy:
            predicate += """ OR COALESCE(json_extract(attrs_json, '$."reflect.telemetry.origin"'), '')
                IS NOT COALESCE(origin_kind, '')"""
        rows = conn.execute(
            f"""
            SELECT id, session_id, source_type, origin_kind, attrs_json
            FROM raw_events
            WHERE {predicate}
            ORDER BY observed_at, id
            """
        ).fetchall()
        repaired_raw_events = 0
        repaired_steps = 0
        repaired_session_ids: set[str] = set()
        for row in rows:
            attrs = _load_json(row["attrs_json"])
            origin_kind = classify_origin_kind(str(row["source_type"] or ""), attrs)
            if not origin_kind:
                continue
            repaired_attrs = apply_origin_kind(attrs, origin_kind)
            attrs_json = json.dumps(repaired_attrs, sort_keys=True)
            conn.execute(
                "UPDATE raw_events SET origin_kind = ?, attrs_json = ? WHERE id = ?",
                (origin_kind, attrs_json, row["id"]),
            )
            repaired_raw_events += 1
            step_id = _stable_id("step", row["id"])
            step_result = conn.execute(
                "UPDATE steps SET origin_kind = ?, "
                "raw_attrs_json = json_set(raw_attrs_json, '$.\"reflect.telemetry.origin\"', ?) WHERE id = ?",
                (origin_kind, origin_kind, step_id),
            )
            repaired_steps += int(step_result.rowcount or 0)
            if step_result.rowcount and row["session_id"]:
                repaired_session_ids.add(str(row["session_id"]))
        PendingRefreshState(conn).mark(repaired_session_ids)
        if legacy:
            conn.execute(
                """INSERT INTO store_metadata(key, value, updated_at) VALUES (?, 'complete', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
                (_PROVENANCE_REPAIR_KEY, _now()),
            )
        conn.commit()
        savepoint_started = False
        return {"raw_events": repaired_raw_events, "steps": repaired_steps}
    except BaseException:
        if savepoint_started:
            conn.execute("ROLLBACK TO SAVEPOINT reflect_provenance_repair")
            conn.execute("RELEASE SAVEPOINT reflect_provenance_repair")
        if owned_transaction:
            conn.rollback()
        raise
    finally:
        conn.row_factory = previous_row_factory


def _upsert_tool_call(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    timestamp: str,
    status: str,
    duration_ms: int | None,
) -> str:
    logical_call_id = _logical_tool_call_id(attrs, raw_event["id"])
    if _is_codex_dispatch(attrs):
        # A generic Codex call_id is scoped by its session, but old/corrupt
        # captures can still reuse it across turns or names. Keep that
        # observation separate rather than joining incompatible invocations.
        existing = conn.execute(
            "SELECT raw_attrs_json,tool_name,tool_type FROM tool_calls WHERE session_id=? AND logical_call_id=?",
            (session_id, logical_call_id),
        ).fetchone()
        if existing:
            prior_attrs = _load_json(existing[0])
            prior_turn = _first_text(prior_attrs, "turn.id", "turn_id")
            current_turn = _first_text(attrs, "turn.id", "turn_id")
            prior_name = recorded_tool_name(prior_attrs) or existing[1]
            current_name = recorded_tool_name(attrs)
            if (
                prior_turn != current_turn
                or (
                    prior_name != current_name
                    and existing[2] != "mcp"
                )
            ):
                logical_call_id = str(raw_event["id"])
    tool_name = _canonical_tool_name(attrs, raw_event["event_type"])
    identity = DEFAULT_MCP_CLASSIFIER.identify(attrs)
    input_preview = _first_text(attrs, "gen_ai.client.tool.input", "tool.input")
    output_preview = _first_text(attrs, "gen_ai.client.tool.output", "tool.output")
    recorded_name_sql = "COALESCE(" + ",".join(
        f"NULLIF(trim(json_extract(tool_calls.raw_attrs_json, '$.\"{key}\"')), '')" for key in TOOL_NAME_KEYS
    ) + ")"
    conn.execute(
        f"""
        INSERT OR IGNORE INTO tool_calls(
          id, step_id, session_id, logical_call_id, tool_name, tool_type, status, duration_ms,
          input_hash, output_hash, input_preview_redacted, output_preview_redacted, error_type,
          error_message_redacted, raw_attrs_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, logical_call_id)
          WHERE logical_call_id IS NOT NULL AND logical_call_id <> ''
        DO UPDATE SET
          tool_name = CASE WHEN ? AND tool_calls.tool_name IS NOT excluded.tool_name
            THEN tool_calls.tool_name
            WHEN ? OR (? AND {recorded_name_sql} IS NULL AND tool_calls.tool_type IS NOT 'mcp')
            THEN excluded.tool_name ELSE tool_calls.tool_name END,
          tool_type = COALESCE(excluded.tool_type, tool_calls.tool_type),
          status = CASE
            WHEN excluded.status = 'error' OR tool_calls.status = 'error'
              OR NULLIF(trim(tool_calls.error_type), '') IS NOT NULL THEN 'error'
            WHEN excluded.status = 'ok' OR tool_calls.status = 'ok' THEN 'ok'
            ELSE excluded.status
          END,
          duration_ms = CASE WHEN ? AND tool_calls.duration_ms IS NOT NULL
            THEN tool_calls.duration_ms ELSE MAX(
              COALESCE(tool_calls.duration_ms, 0),
              COALESCE(excluded.duration_ms, 0)
            ) END,
          input_hash = COALESCE(tool_calls.input_hash, excluded.input_hash),
          output_hash = COALESCE(excluded.output_hash, tool_calls.output_hash),
          input_preview_redacted = COALESCE(
            tool_calls.input_preview_redacted,
            excluded.input_preview_redacted
          ),
          output_preview_redacted = COALESCE(
            excluded.output_preview_redacted,
            tool_calls.output_preview_redacted
          ),
          error_type = COALESCE(excluded.error_type, tool_calls.error_type),
          error_message_redacted = COALESCE(
            excluded.error_message_redacted,
            tool_calls.error_message_redacted
          ),
          raw_attrs_json = json_patch(tool_calls.raw_attrs_json, excluded.raw_attrs_json),
          updated_at = excluded.updated_at
        """,
        (
            _stable_id("tool", raw_event["id"]),
            step_id,
            session_id,
            logical_call_id,
            tool_name,
            "mcp" if identity.server_name else attrs.get("gen_ai.client.tool_type"),
            status,
            duration_ms,
            _first_hash(
                attrs,
                input_preview,
                "gen_ai.client.tool.input.sha256",
                "gen_ai.client.tool.input_hash",
                "tool.input.sha256",
            ),
            _first_hash(
                attrs,
                output_preview,
                "gen_ai.client.tool.output.sha256",
                "gen_ai.client.tool.output_hash",
                "tool.output.sha256",
            ),
            input_preview,
            output_preview,
            attrs.get("error.type"),
            _first_text(attrs, "gen_ai.client.error.text", "error.message"),
            json.dumps(attrs, sort_keys=True),
            timestamp,
            timestamp,
            _is_codex_dispatch(attrs),
            bool(recorded_tool_name(attrs) or identity.server_name and identity.tool_name),
            bool(attrs.get("gen_ai.client.command")),
            _is_codex_dispatch(attrs),
        ),
    )
    row = conn.execute(
        """
        SELECT id, step_id, status
        FROM tool_calls
        WHERE session_id = ? AND logical_call_id = ?
        """,
        (session_id, logical_call_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("canonical tool call was not persisted")
    refresh_command_projection(conn, str(row[0]))
    if row[2] == "error":
        conn.execute(
            "UPDATE steps SET status = 'error' WHERE id IN (?, ?)", (row[1], step_id),
        )
    return str(row[0])


def _upsert_mcp_call_details(
    conn: sqlite3.Connection,
    *,
    tool_call_id: str,
    attrs: dict[str, Any],
    timestamp: str,
) -> None:
    identity = DEFAULT_MCP_CLASSIFIER.identify(attrs)
    conn.execute(
        """
        INSERT INTO mcp_calls(
          tool_call_id, mcp_session_id, mcp_protocol_version, transport,
          server_name, tool_name, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tool_call_id) DO UPDATE SET
          server_name = COALESCE(mcp_calls.server_name, excluded.server_name),
          tool_name = COALESCE(mcp_calls.tool_name, excluded.tool_name),
          mcp_session_id = COALESCE(mcp_calls.mcp_session_id, excluded.mcp_session_id),
          mcp_protocol_version = COALESCE(
            mcp_calls.mcp_protocol_version,
            excluded.mcp_protocol_version
          ),
          transport = COALESCE(mcp_calls.transport, excluded.transport),
          updated_at = excluded.updated_at
        """,
        (
            tool_call_id,
            DEFAULT_MCP_CLASSIFIER.session_id(attrs),
            DEFAULT_MCP_CLASSIFIER.protocol_version(attrs),
            DEFAULT_MCP_CLASSIFIER.transport(attrs),
            identity.server_name,
            identity.tool_name or attrs.get("gen_ai.client.tool_name"),
            timestamp,
            timestamp,
        ),
    )


def _insert_call_record(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    step_type: str,
    timestamp: str,
) -> None:
    duration = _duration_ms(raw_event["observed_at"], raw_event["received_at"])
    status = _status(raw_event["event_type"], attrs)
    # Agent lifecycle and model usage are independent facts. A token-bearing
    # SubagentStop remains an agent_call step while owning canonical usage.
    if step_type == "llm_call" or (step_type == "agent_call" and _has_token_attributes(attrs)):
        request_id = attrs.get("gen_ai.request.id")
        call_id = (
            _stable_id("llm", session_id, raw_event["source_id"], str(request_id))
            if request_id and raw_event["source_type"] == "native_session"
            else _stable_id("llm", raw_event["id"])
        )
        prompt_preview = _first_text(
            attrs,
            "gen_ai.client.prompt.text",
            "gen_ai.client.prompt",
            "gen_ai.input.messages",
            "prompt",
        )
        response_preview = _first_text(
            attrs,
            "gen_ai.client.response.text",
            "gen_ai.client.stop_message.text",
            "gen_ai.client.output",
            "gen_ai.response.text",
            "gen_ai.response.content",
            "gen_ai.output.messages",
            "response",
        )
        conn.execute(
            """
            INSERT INTO llm_calls(
              id, step_id, session_id, provider, request_model, response_model,
              operation_name, input_tokens, output_tokens, cache_creation_input_tokens,
              cache_read_input_tokens, reasoning_output_tokens, latency_ms,
              prompt_hash, response_hash, prompt_preview_redacted, response_preview_redacted,
              raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              step_id = excluded.step_id,
              request_model = excluded.request_model,
              response_model = excluded.response_model,
              input_tokens = excluded.input_tokens,
              output_tokens = excluded.output_tokens,
              cache_creation_input_tokens = excluded.cache_creation_input_tokens,
              cache_read_input_tokens = excluded.cache_read_input_tokens,
              reasoning_output_tokens = excluded.reasoning_output_tokens,
              raw_attrs_json = excluded.raw_attrs_json,
              response_hash = excluded.response_hash,
              response_preview_redacted = excluded.response_preview_redacted,
              updated_at = excluded.updated_at
            """,
            (
                call_id,
                step_id,
                session_id,
                attrs.get("gen_ai.system"),
                attrs.get("gen_ai.request.model"),
                attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model"),
                _event_name(raw_event["event_type"], attrs),
                _to_int(attrs.get("gen_ai.usage.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.output_tokens")),
                _to_int(attrs.get("gen_ai.usage.cache_creation.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.cache_read.input_tokens")),
                _to_int(attrs.get("gen_ai.usage.reasoning_output_tokens")),
                duration,
                _first_hash(
                    attrs,
                    prompt_preview,
                    "gen_ai.client.prompt.sha256",
                    "gen_ai.prompt.sha256",
                ),
                _first_hash(
                    attrs,
                    response_preview,
                    "gen_ai.client.response.sha256",
                    "gen_ai.client.stop_message.sha256",
                    "gen_ai.response.sha256",
                ),
                prompt_preview,
                response_preview,
                json.dumps(attrs, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
    elif step_type in {"tool_call", "mcp_call", "shell_command"}:
        tool_call_id = _upsert_tool_call(
            conn,
            raw_event=raw_event,
            attrs=attrs,
            session_id=session_id,
            step_id=step_id,
            timestamp=timestamp,
            status=status,
            duration_ms=duration,
        )
        identity = DEFAULT_MCP_CLASSIFIER.identify(attrs)
        if step_type == "mcp_call" or identity.server_name:
            _upsert_mcp_call_details(
                conn,
                tool_call_id=tool_call_id,
                attrs=attrs,
                timestamp=timestamp,
            )


def _insert_memory_record(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    timestamp: str,
) -> None:
    memory_id = attrs.get("gen_ai.memory.id")
    if not memory_id:
        return
    from reflect.memory.models import MemorySourceMetadata

    source_path = str(attrs.get("gen_ai.memory.source_path") or attrs.get("path") or "")
    source = MemorySourceMetadata(
        source_kind=str(attrs.get("gen_ai.memory.source") or "opentelemetry_hook"),
        source_ref=source_path or str(memory_id),
        path=source_path,
        workspace_root=str(attrs.get("code.workspace.root") or attrs.get("workspace_root") or ""),
        content_hash=str(attrs.get("gen_ai.memory.content_hash") or ""),
    )
    conn.execute(
        """
        INSERT INTO memories(
          id, scope, type, session_id, step_id, content_hash,
          content_preview_redacted, confidence, sensitivity, source, expires_at,
          last_seen_at, raw_attrs_json, source_metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          content_hash = CASE WHEN memories.source = 'filesystem_instruction_scan'
            THEN memories.content_hash ELSE COALESCE(excluded.content_hash, memories.content_hash) END,
          content_preview_redacted = CASE WHEN memories.source = 'filesystem_instruction_scan'
            THEN memories.content_preview_redacted ELSE COALESCE(
            excluded.content_preview_redacted,
            memories.content_preview_redacted
          ) END,
          source_metadata_json = CASE WHEN memories.source = 'filesystem_instruction_scan'
            THEN memories.source_metadata_json
            ELSE json_patch(memories.source_metadata_json, excluded.source_metadata_json) END,
          last_seen_at = CASE
            WHEN memories.last_seen_at IS NULL THEN excluded.last_seen_at
            WHEN excluded.last_seen_at IS NULL THEN memories.last_seen_at
            ELSE MAX(memories.last_seen_at, excluded.last_seen_at)
          END,
          updated_at = excluded.updated_at
        """,
        (
            memory_id,
            attrs.get("gen_ai.memory.scope") or "agent",
            attrs.get("gen_ai.memory.type") or "unknown",
            session_id,
            step_id,
            attrs.get("gen_ai.memory.content_hash"),
            attrs.get("gen_ai.memory.content_preview"),
            float(attrs.get("gen_ai.memory.confidence") or 0.5),
            attrs.get("gen_ai.memory.sensitivity") or "unknown",
            attrs.get("gen_ai.memory.source") or "opentelemetry_hook",
            attrs.get("gen_ai.memory.expires_at"),
            attrs.get("gen_ai.memory.last_seen_at") or raw_event["observed_at"],
            raw_event["attrs_json"],
            _json_dumps(source.to_json_dict()),
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO memory_exposures(
          memory_id, session_id, step_id, content_hash, source, observed_at,
          created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(memory_id, session_id) DO UPDATE SET
          step_id = excluded.step_id,
          content_hash = excluded.content_hash,
          source = excluded.source,
          observed_at = excluded.observed_at,
          updated_at = excluded.updated_at
        """,
        (
            memory_id,
            session_id,
            step_id,
            attrs.get("gen_ai.memory.content_hash"),
            attrs.get("gen_ai.memory.source") or "opentelemetry_hook",
            attrs.get("gen_ai.memory.last_seen_at") or raw_event["observed_at"],
            timestamp,
            timestamp,
        ),
    )


def _insert_privacy_finding(
    conn: sqlite3.Connection,
    *,
    raw_event: sqlite3.Row,
    attrs: dict[str, Any],
    session_id: str,
    step_id: str,
    timestamp: str,
) -> None:
    finding_type = attrs.get("gen_ai.privacy.finding_type")
    if not finding_type:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO privacy_findings(
          id, session_id, step_id, finding_type, severity, field_name,
          action_taken, detail_redacted, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            _stable_id("privacy", raw_event["id"], finding_type),
            session_id,
            step_id,
            finding_type,
            attrs.get("gen_ai.privacy.severity") or "unknown",
            attrs.get("gen_ai.privacy.field_name"),
            attrs.get("gen_ai.privacy.action_taken") or "detected",
            attrs.get("gen_ai.privacy.detail_redacted"),
            timestamp,
        ),
    )


def _backfill_parent_step_ids(conn: sqlite3.Connection, session_ids: set[str]) -> None:
    for session_id in sorted(session_ids):
        parents = dict(conn.execute(
            "SELECT id,parent_step_id FROM steps WHERE session_id=?", (session_id,),
        ))
        rows = conn.execute(
            "SELECT id,span_id,parent_span_id FROM raw_events "
            "WHERE session_id=? AND COALESCE(span_id,'')<>''", (session_id,),
        ).fetchall()
        span_to_step_id = {str(row["span_id"]): _stable_id("step", row["id"]) for row in rows}
        links = []
        for row in rows:
            if not row["parent_span_id"]:
                continue
            step_id = _stable_id("step", row["id"])
            parent_id = span_to_step_id.get(str(row["parent_span_id"]))
            if (step_id in parents and parents[step_id] is None
                    and parent_id in parents and parent_id != step_id):
                links.append((parent_id, step_id))
        # Existing links are immutable here. Only unresolved links whose parent
        # is now present need writes; late parents use the same path.
        conn.executemany(
            "UPDATE steps SET parent_step_id=? WHERE id=? AND parent_step_id IS NULL", links,
        )
        if links:
            PendingRefreshState(conn).mark_steps(
                session_id, (step_id for _, step_id in links), phases=("graph", "improvements"),
            )


def refresh_session_usage(conn: sqlite3.Connection, session_ids: set[str]) -> None:
    """Reconcile canonical observations with the pricing/reader identity rule."""
    keys = ("gen_ai.request.id", "reflect.usage.identity", "reflect.usage.input_semantics",
            *_TOKEN_ATTRIBUTE_KEYS, "hook.cache_read_tokens", "hook.cache_write_tokens")
    paths = ",".join(f"'$.\"{key}\"'" for key in keys)
    for session_id in session_ids:
        # Keep retained hook/request row IDs and source provenance intact.
        # Multiple observations of one explicit request own one subtotal, in
        # the same chronological order used by pricing and measurements.
        cursor = conn.execute(
            f"""SELECT lc.id,lc.session_id,lc.provider,lc.input_tokens,lc.output_tokens,
                      lc.cache_creation_input_tokens,lc.cache_read_input_tokens,lc.reasoning_output_tokens,
                      json_extract(lc.raw_attrs_json,{paths}) AS usage_attrs_json
               FROM llm_calls lc JOIN steps st ON st.id=lc.step_id
               WHERE lc.session_id=? ORDER BY st.started_at,lc.id""", (session_id,),
        )
        names = [column[0] for column in cursor.description]
        totals = dict.fromkeys(("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                "cache_read_input_tokens", "reasoning_output_tokens"), 0)
        seen: set[tuple] = set()
        for values in cursor:
            row = dict(zip(names, values, strict=True))
            attrs = dict(zip(keys, json.loads(row["usage_attrs_json"]), strict=True))
            facts = usage_facts(row, attrs)
            if facts.present and facts.identity not in seen:
                seen.add(facts.identity)
                for key in totals:
                    totals[key] += facts.tokens[key]
        updated = conn.execute(
            """
            UPDATE sessions SET
              (input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, reasoning_tokens)
              = (?, ?, ?, ?, ?)
            WHERE id = ? AND token_provenance <> 'estimated_cursor_transcript'
              AND (input_tokens, output_tokens, cache_creation_tokens, cache_read_tokens, reasoning_tokens)
                  IS NOT (?, ?, ?, ?, ?)
            """,
            (*totals.values(), session_id, *totals.values()),
        )
        if updated.rowcount:
            PendingRefreshState(conn).mark({session_id}, phases=("cost", "rollup"))


def _refresh_session_statuses(conn: sqlite3.Connection, session_ids: set[str], timestamp: str) -> None:
    if not session_ids:
        return
    for session_id in sorted(session_ids):
        row = conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM tool_calls WHERE session_id = ? AND status = 'error')
              + COALESCE(SUM(CASE WHEN status = 'error'
                AND type NOT IN ('tool_call', 'mcp_call', 'shell_command')
                THEN 1 ELSE 0 END), 0) AS error_count,
              COALESCE(SUM(CASE
                WHEN lower(COALESCE(json_extract(raw_attrs_json, '$."gen_ai.client.hook.event"'), summary, ''))
                  IN ('stop', 'sessionend', 'session_end', 'subagentstop', 'subagent_stop')
                  THEN 1
                WHEN lower(COALESCE(summary, '')) IN (
                  'gen_ai.client.hook.stop',
                  'gen_ai.client.hook.sessionend',
                  'gen_ai.client.hook.subagentstop',
                  'ide.hook.stop'
                )
                  THEN 1
                ELSE 0
              END), 0) AS terminal_count
            FROM steps
            WHERE session_id = ?
            """,
            (session_id, session_id),
        ).fetchone()
        if not row:
            continue
        error_count = int(row["error_count"] or 0)
        terminal_count = int(row["terminal_count"] or 0)
        if error_count > 0:
            status = "error"
        elif terminal_count > 0:
            status = "ok"
        else:
            status = "unknown"
        conn.execute(
            """
            UPDATE sessions
            SET status = ?, failure_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, error_count, timestamp, session_id),
        )


def refresh_session_statuses(
    conn: sqlite3.Connection,
    session_ids: set[str],
) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        normalized_ids = {str(session_id) for session_id in session_ids if session_id}
        _refresh_session_statuses(conn, normalized_ids, _now())
        return {"sessions": len(normalized_ids)}
    finally:
        conn.row_factory = previous_row_factory


def refresh_all_session_statuses(conn: sqlite3.Connection) -> dict[str, int]:
    session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions").fetchall()}
    return refresh_session_statuses(conn, session_ids)


def normalize_pending_raw_events(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    changed_session_ids: set[str] | None = None,
    batch_size: int = 500,
) -> dict[str, int]:
    if batch_size < 1 or (limit is not None and limit < 0):
        raise ValueError("batch_size must be positive and limit must be nonnegative")
    repair_telemetry_provenance(conn, incremental=True)
    result = {"processed": 0, "failed": 0, "skipped": 0}
    normalized_session_ids: set[str] = set()
    remaining = limit
    while remaining is None or remaining > 0:
        size = batch_size if remaining is None else min(batch_size, remaining)
        batch = _normalize_pending_batch(conn, limit=size, changed_session_ids=normalized_session_ids)
        for key in result:
            result[key] += batch[key]
        count = batch["processed"] + batch["failed"] + batch["skipped"]
        if count < size:
            break
        if remaining is not None:
            remaining -= count
    normalized_session_ids.update(_refresh_normalized_sessions(conn, normalized_session_ids))
    if changed_session_ids is not None:
        changed_session_ids.update(normalized_session_ids)
    return result


def _refresh_normalized_sessions(conn: sqlite3.Connection, session_ids: set[str]) -> set[str]:
    """Finish session-wide maintenance once, including interrupted raw batches.

    Raw batches commit their dirty scope with canonical rows. Hold the writer
    while maintaining and acknowledging that scope; failures retain it for the
    next invocation even when no raw events remain pending.
    """
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        pending = PendingRefreshState(conn)
        captured = pending.capture()
        session_ids = session_ids | captured.pending_for("canonical")
        repair_identities = conn.execute(
            "SELECT 1 FROM maintenance_tasks WHERE task = 'reconcile_task_identities'"
        ).fetchone()
        if not session_ids and not repair_identities:
            conn.commit()
            return set()
        timestamp = _now()
        _backfill_parent_step_ids(conn, session_ids)
        backfill_session_context(
            conn, timestamp=timestamp, changed_session_ids=session_ids, session_ids=session_ids,
        )
        # New/changed steps already persist their facts with their canonical
        # transaction. Only explicit broad repairs need historical JSON again.
        full_fact_sessions = {sid for sid, steps in captured.step_scope("canonical").items() if steps is None}
        if full_fact_sessions:
            backfill_hook_facts(conn, session_ids=full_fact_sessions, timestamp=timestamp)
        _refresh_session_statuses(conn, session_ids, timestamp)
        refresh_session_usage(conn, session_ids)
        reconciler = TaskRunReconciler(conn)
        recovered_ids = reconciler.recover_identities(None if repair_identities else session_ids)
        pending.mark(recovered_ids)
        session_ids.update(recovered_ids)
        if repair_identities:
            conn.execute("DELETE FROM maintenance_tasks WHERE task = 'reconcile_task_identities'")
        reconciler.reconcile(session_ids=session_ids, commit=False)
        # Git evidence is a canonical-call projection shared by usage/report
        # refreshes. Its rows and the acknowledgement must commit together so
        # an interrupted refresh can retry even after raw batches were drained.
        from reflect.store.git_activity import refresh_git_activity

        refresh_git_activity(conn, session_ids=session_ids)
        # Context/identity repair can record additional revisions in this same
        # transaction. They are also maintained; no other writer can intervene.
        pending.acknowledge(pending.capture(), "canonical", session_ids=session_ids)
        conn.commit()
        return session_ids
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous_row_factory


def _normalize_pending_batch(
    conn: sqlite3.Connection, *, limit: int, changed_session_ids: set[str] | None,
) -> dict[str, int]:
    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        # Reserve the writer before reading the pending set. Per-event savepoints
        # must not commit independently or allow another normalizer to select
        # the same events between reads and writes.
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT *
            FROM raw_events
            WHERE normalized_status = 'pending'
            ORDER BY observed_at, id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        processed = 0
        failed = 0
        skipped = 0
        processed_session_ids: set[str] = set()
        changed_steps: dict[str, set[str]] = {}
        hook_facts = HookFactRepository(conn)
        timestamp = _now()
        for index, row in enumerate(rows):
            savepoint = f"normalize_raw_event_{index}"
            session_id = ""
            try:
                conn.execute(f"SAVEPOINT {savepoint}")
                attrs = _canonical_usage_attrs(with_tool_outcome(row["event_type"], _load_json(row["attrs_json"])))
                if attrs.get("reflect.usage.enrichment_only"):
                    attrs = {key: value for key, value in attrs.items() if not key.startswith("gen_ai.usage.")}
                classification = unassigned_codex_classification(attrs)
                if not row["session_id"] and classification:
                    attrs["reflect.telemetry.classification"] = classification
                    conn.execute(
                        "UPDATE raw_events SET normalized_status = 'ignored', "
                        "normalization_error = NULL, attrs_json = ? WHERE id = ?",
                        (json.dumps(attrs, sort_keys=True), row["id"]),
                    )
                    conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                    skipped += 1
                    continue
                session_id = row["session_id"] or telemetry_session_id(attrs)
                if not session_id:
                    session_id = _stable_id("session", row["source_id"])
                session_id = str(session_id)
                agent_id = _insert_agent(conn, attrs, timestamp)
                _upsert_session(
                    conn,
                    session_id=session_id,
                    agent_id=agent_id,
                    raw_event=row,
                    attrs=attrs,
                    timestamp=timestamp,
                )
                step_type = _step_type(row["event_type"], attrs)
                step_id = _insert_step(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_type=step_type,
                    timestamp=timestamp,
                )
                _insert_call_record(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    step_type=step_type,
                    timestamp=timestamp,
                )
                _insert_memory_record(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    timestamp=timestamp,
                )
                _insert_privacy_finding(
                    conn,
                    raw_event=row,
                    attrs=attrs,
                    session_id=session_id,
                    step_id=step_id,
                    timestamp=timestamp,
                )
                hook_facts.persist_step(
                    step_id=step_id, session_id=session_id,
                    fallback_event=row["event_type"], attrs=attrs, timestamp=timestamp,
                )
                event_steps = {step_id}
                if step_type in {"tool_call", "mcp_call", "shell_command"}:
                    paired = conn.execute(
                        "SELECT step_id FROM tool_calls WHERE session_id=? AND logical_call_id=?",
                        (session_id, _logical_tool_call_id(attrs, row["id"])),
                    ).fetchone()
                    if paired:
                        event_steps.add(str(paired[0]))
                conn.execute(
                    "UPDATE raw_events SET normalized_status = 'ok', normalization_error = NULL WHERE id = ?",
                    (row["id"],),
                )
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                changed_steps.setdefault(session_id, set()).update(event_steps)
                processed_session_ids.add(session_id)
                processed += 1
            except Exception as exc:
                conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {savepoint}")
                if isinstance(exc, sqlite3.OperationalError):
                    # Store failures are not malformed telemetry. Keep events
                    # pending and preserve the original error for the caller.
                    raise
                conn.execute(
                    "UPDATE raw_events SET normalized_status = 'failed', normalization_error = ? WHERE id = ?",
                    (str(exc), row["id"]),
                )
                failed += 1
        if changed_session_ids is not None:
            changed_session_ids.update(processed_session_ids)
        pending = PendingRefreshState(conn)
        for session_id, step_ids in changed_steps.items():
            pending.mark_steps(session_id, step_ids)
        conn.commit()
        return {"processed": processed, "failed": failed, "skipped": skipped}
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.row_factory = previous_row_factory
