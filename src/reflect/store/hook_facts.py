from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


def _stable_id(prefix: str, *parts: object) -> str:
    payload = ":".join(str(part or "") for part in parts)
    return f"{prefix}_{hashlib.sha1(payload.encode()).hexdigest()}"


def _text(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _integer(attrs: dict[str, Any], key: str) -> int:
    value = attrs.get(key)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


@dataclass(frozen=True)
class ConversationFact:
    kind: str
    role: str
    content_hash: str | None
    content_length: int
    preview: str | None


@dataclass(frozen=True)
class HookFactContract:
    event_name: str
    event_id: str | None
    event_id_source: str | None
    telemetry_source: str | None
    schema_version: int | None
    provider_adapter: str | None
    original_event: str | None
    native_trace_id: str | None
    native_span_id: str | None
    native_parent_span_id: str | None
    agent_id: str | None
    parent_agent_id: str | None
    conversation: tuple[ConversationFact, ...]

    @property
    def has_facts(self) -> bool:
        """Return whether the span contains data owned by the hook fact contract."""
        return bool(
            self.event_id
            or self.event_id_source
            or self.telemetry_source
            or self.schema_version is not None
            or self.provider_adapter
            or self.original_event
            or self.native_trace_id
            or self.native_span_id
            or self.native_parent_span_id
            or self.agent_id
            or self.parent_agent_id
            or self.conversation
            or self.event_name in {"SubagentStart", "SubagentStop"}
        )


class HookFactParser:
    """Parse the opentelemetry-hooks fact contract without coupling to a provider."""

    _conversation_roles = {
        "prompt": "user",
        "response": "assistant",
        "stop_message": "system",
        "error": "error",
        "delegation.task": "system",
    }

    def parse(self, attrs: dict[str, Any], fallback_event: str = "") -> HookFactContract:
        event_name = str(
            attrs.get("gen_ai.client.hook.event")
            or attrs.get("ide.hook.event")
            or fallback_event
        ).rsplit(".", 1)[-1]
        schema_value = attrs.get("gen_ai.client.hook_schema_version")
        try:
            schema_version = int(schema_value) if schema_value is not None else None
        except (TypeError, ValueError):
            schema_version = None
        conversation = tuple(
            fact
            for kind, role in self._conversation_roles.items()
            if (fact := self._conversation_fact(attrs, kind, role)) is not None
        )
        return HookFactContract(
            event_name=event_name,
            event_id=_text(attrs, "gen_ai.client.hook.event_id"),
            event_id_source=_text(attrs, "gen_ai.client.hook.event_id_source"),
            telemetry_source=_text(attrs, "gen_ai.client.telemetry_source"),
            schema_version=schema_version,
            provider_adapter=_text(attrs, "gen_ai.client.hook.provider_adapter"),
            original_event=_text(attrs, "gen_ai.client.hook.original_event"),
            native_trace_id=_text(attrs, "gen_ai.client.native_trace_id"),
            native_span_id=_text(attrs, "gen_ai.client.native_span_id"),
            native_parent_span_id=_text(attrs, "gen_ai.client.native_parent_span_id"),
            agent_id=_text(attrs, "gen_ai.client.agent_id", "gen_ai.agent.id"),
            parent_agent_id=_text(attrs, "gen_ai.client.parent_agent_id"),
            conversation=conversation,
        )

    @staticmethod
    def _conversation_fact(
        attrs: dict[str, Any],
        kind: str,
        role: str,
    ) -> ConversationFact | None:
        prefix = f"gen_ai.client.{kind}"
        preview = _text(attrs, f"{prefix}.text")
        content_hash = _text(attrs, f"{prefix}.sha256")
        content_length = _integer(attrs, f"{prefix}.length")
        if preview and not content_hash:
            content_hash = hashlib.sha256(preview.encode()).hexdigest()
        if preview and not content_length:
            content_length = len(preview)
        if not preview and not content_hash and not content_length:
            return None
        return ConversationFact(kind, role, content_hash, content_length, preview)


@dataclass(frozen=True)
class HookFactSessionView:
    """Read model for hook facts associated with one session."""

    conversation_by_step: dict[str, tuple[dict[str, object], ...]]
    agent_events_by_step: dict[str, dict[str, object]]

    def facts_for_step(self, step_id: object) -> tuple[dict[str, object], ...]:
        return self.conversation_by_step.get(str(step_id), ())

    def prompt_for_step(self, step_id: object) -> dict[str, object] | None:
        return next(
            (fact for fact in self.facts_for_step(step_id) if fact.get("kind") == "prompt"),
            None,
        )

    def responses_for_step(self, step_id: object) -> tuple[dict[str, object], ...]:
        facts = self.facts_for_step(step_id)
        responses = tuple(fact for fact in facts if fact.get("kind") == "response")
        if responses:
            return responses
        return tuple(fact for fact in facts if fact.get("kind") == "stop_message")

    def agent_event_for_step(self, step_id: object) -> dict[str, object] | None:
        return self.agent_events_by_step.get(str(step_id))

    def agent_types_by_id(self) -> dict[str, str]:
        return {
            str(event["agent_id"]): str(event.get("agent_type") or "unknown")
            for event in self.agent_events_by_step.values()
            if event.get("agent_id")
        }

    def summary(self, steps: Iterable[Mapping[str, object]]) -> dict[str, object]:
        step_rows = tuple(steps)
        return {
            "hook_schema_versions": sorted({
                int(step["hook_schema_version"])
                for step in step_rows
                if step.get("hook_schema_version") is not None
            }),
            "provider_adapters": sorted({
                str(step["hook_provider_adapter"])
                for step in step_rows
                if step.get("hook_provider_adapter")
            }),
            "telemetry_sources": sorted({
                str(step["telemetry_source"])
                for step in step_rows
                if step.get("telemetry_source")
            }),
            "native_linked_spans": sum(1 for step in step_rows if step.get("native_trace_id")),
            "conversation_facts": sum(
                len(facts) for facts in self.conversation_by_step.values()
            ),
            "agent_relationships": len(self.agent_events_by_step),
        }


class HookFactRepository:
    """Persist and query hook facts while retaining complete source attributes."""

    def __init__(self, conn: sqlite3.Connection, parser: HookFactParser | None = None) -> None:
        self.conn = conn
        self.parser = parser or HookFactParser()

    def persist_step(
        self,
        *,
        step_id: str,
        session_id: str,
        fallback_event: str,
        attrs: dict[str, Any],
        timestamp: str,
    ) -> bool:
        contract = self.parser.parse(attrs, fallback_event)
        if not contract.has_facts:
            return False
        self.conn.execute(
            """
            UPDATE steps
            SET hook_event_id = ?, hook_event_id_source = ?, telemetry_source = ?,
                hook_schema_version = ?, hook_provider_adapter = ?, original_event = ?,
                native_trace_id = ?, native_span_id = ?, native_parent_span_id = ?,
                agent_invocation_id = ?, parent_agent_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                contract.event_id,
                contract.event_id_source,
                contract.telemetry_source,
                contract.schema_version,
                contract.provider_adapter,
                contract.original_event,
                contract.native_trace_id,
                contract.native_span_id,
                contract.native_parent_span_id,
                contract.agent_id,
                contract.parent_agent_id,
                timestamp,
                step_id,
            ),
        )
        raw_attrs = json.dumps(attrs, sort_keys=True)
        for fact in contract.conversation:
            self.conn.execute(
                """
                INSERT INTO conversation_facts(
                  id, step_id, session_id, kind, role, content_hash, content_length,
                  content_preview_redacted, raw_attrs_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(step_id, kind, role) DO UPDATE SET
                  content_hash = COALESCE(excluded.content_hash, conversation_facts.content_hash),
                  content_length = MAX(conversation_facts.content_length, excluded.content_length),
                  content_preview_redacted = COALESCE(
                    excluded.content_preview_redacted,
                    conversation_facts.content_preview_redacted
                  ),
                  raw_attrs_json = excluded.raw_attrs_json,
                  updated_at = excluded.updated_at
                """,
                (
                    _stable_id("message", step_id, fact.kind, fact.role),
                    step_id,
                    session_id,
                    fact.kind,
                    fact.role,
                    fact.content_hash,
                    fact.content_length,
                    fact.preview,
                    raw_attrs,
                    timestamp,
                    timestamp,
                ),
            )
        if contract.event_name not in {"SubagentStart", "SubagentStop"}:
            return True
        self._persist_agent_event(contract, step_id, session_id, attrs, raw_attrs, timestamp)
        return True

    def load_session(self, session_id: str) -> HookFactSessionView:
        conversation_by_step: dict[str, list[dict[str, object]]] = defaultdict(list)
        for fact in self._query_dicts(
            """
            SELECT *
            FROM conversation_facts
            WHERE session_id = ?
            ORDER BY created_at, id
            """,
            (session_id,),
        ):
            conversation_by_step[str(fact["step_id"])].append(fact)
        agent_events_by_step: dict[str, dict[str, object]] = {}
        for event in self._query_dicts(
            """
            SELECT *
            FROM agent_events
            WHERE session_id = ?
            ORDER BY created_at, id
            """,
            (session_id,),
        ):
            agent_events_by_step[str(event["step_id"])] = event
        return HookFactSessionView(
            conversation_by_step={
                step_id: tuple(facts) for step_id, facts in conversation_by_step.items()
            },
            agent_events_by_step=agent_events_by_step,
        )

    def _query_dicts(
        self,
        query: str,
        params: tuple[object, ...],
    ) -> list[dict[str, object]]:
        cursor = self.conn.execute(query, params)
        columns = tuple(column[0] for column in cursor.description or ())
        return [
            dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row, strict=True))
            for row in cursor.fetchall()
        ]

    def _persist_agent_event(
        self,
        contract: HookFactContract,
        step_id: str,
        session_id: str,
        attrs: dict[str, Any],
        raw_attrs: str,
        timestamp: str,
    ) -> None:
        task = next(
            (fact for fact in contract.conversation if fact.kind == "delegation.task"),
            None,
        )
        self.conn.execute(
            """
            INSERT INTO agent_events(
              id, step_id, session_id, event_name, event_id, agent_id, parent_agent_id,
              agent_type, agent_id_source, status, task_hash, task_length,
              task_preview_redacted, raw_attrs_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(step_id) DO UPDATE SET
              event_id = COALESCE(excluded.event_id, agent_events.event_id),
              agent_id = COALESCE(excluded.agent_id, agent_events.agent_id),
              parent_agent_id = COALESCE(excluded.parent_agent_id, agent_events.parent_agent_id),
              agent_type = COALESCE(excluded.agent_type, agent_events.agent_type),
              status = COALESCE(excluded.status, agent_events.status),
              task_hash = COALESCE(excluded.task_hash, agent_events.task_hash),
              task_length = MAX(agent_events.task_length, excluded.task_length),
              task_preview_redacted = COALESCE(
                excluded.task_preview_redacted,
                agent_events.task_preview_redacted
              ),
              raw_attrs_json = excluded.raw_attrs_json,
              updated_at = excluded.updated_at
            """,
            (
                _stable_id("agent_event", step_id),
                step_id,
                session_id,
                contract.event_name,
                contract.event_id,
                contract.agent_id,
                contract.parent_agent_id,
                _text(attrs, "gen_ai.client.subagent_type", "gen_ai.agent.name"),
                _text(attrs, "gen_ai.client.agent_id_source"),
                _text(attrs, "gen_ai.client.status"),
                task.content_hash if task else None,
                task.content_length if task else 0,
                task.preview if task else None,
                raw_attrs,
                timestamp,
                timestamp,
            ),
        )


class HookFactBackfill:
    """Idempotently promote raw step attributes into the hook fact contract."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.repository = HookFactRepository(conn)

    def run(
        self,
        *,
        session_ids: set[str] | None = None,
        timestamp: str,
    ) -> dict[str, int]:
        params: list[str] = []
        where = ""
        if session_ids is not None:
            if not session_ids:
                return {"steps": 0, "conversation_facts": 0, "agent_events": 0}
            placeholders = ", ".join("?" for _ in session_ids)
            where = f"WHERE session_id IN ({placeholders})"
            params.extend(sorted(session_ids))
        rows = self.conn.execute(
            "SELECT id, session_id, summary, raw_attrs_json "
            f"FROM steps {where} ORDER BY session_id, seq",
            params,
        ).fetchall()
        before_conversation = self._count("conversation_facts")
        before_agents = self._count("agent_events")
        touched = sum(self._persist_row(row, timestamp) for row in rows)
        return {
            "steps": touched,
            "conversation_facts": self._count("conversation_facts") - before_conversation,
            "agent_events": self._count("agent_events") - before_agents,
        }

    def _persist_row(self, row: sqlite3.Row, timestamp: str) -> int:
        try:
            attrs = json.loads(str(row[3] or "{}"))
        except json.JSONDecodeError:
            return 0
        if not isinstance(attrs, dict):
            return 0
        return int(
            self.repository.persist_step(
                step_id=str(row[0]),
                session_id=str(row[1]),
                fallback_event=str(row[2] or ""),
                attrs=attrs,
                timestamp=timestamp,
            )
        )

    def _count(self, table: str) -> int:
        # Table names are fixed internal constants, never user input.
        return int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def backfill_hook_facts(
    conn: sqlite3.Connection,
    *,
    session_ids: set[str] | None = None,
    timestamp: str,
) -> dict[str, int]:
    """Compatibility boundary for normalization callers."""
    return HookFactBackfill(conn).run(session_ids=session_ids, timestamp=timestamp)
