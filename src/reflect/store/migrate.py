from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def load_migrations() -> list[Migration]:
    migrations_pkg = resources.files("reflect.store.migrations")
    files = sorted(
        p for p in migrations_pkg.iterdir() if p.name.endswith(".sql") and p.name[:3].isdigit()
    )
    loaded: list[Migration] = []
    for file in files:
        version = int(file.name.split("_", 1)[0])
        loaded.append(Migration(version=version, name=file.name, sql=file.read_text(encoding="utf-8")))
    return loaded


def applied_migration_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          applied_at TEXT NOT NULL
        )
        """
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def _migration_statements(sql: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            if pending.strip():
                statements.append(pending)
            pending = ""
    if pending.strip():
        raise ValueError("Migration SQL ended with an incomplete statement")
    return statements


def _repair_tool_evidence(conn: sqlite3.Connection) -> dict[str, int]:
    """One-way migration 50: repair canonical facts without replaying usage.

    Stream small identity attributes first; only actual delegation candidates
    need their retained input/result payloads. No source or accounting rows are
    modified. Dirty step owners reuse the existing downstream refresh pipeline.
    """
    from collections import defaultdict

    from reflect.store.command_patterns import refresh_command_projection
    from reflect.store.hook_facts import HookFactRepository
    from reflect.store.normalize import _canonical_tool_name, _step_type
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.store.provenance import is_codex_otel_service
    from reflect.telemetry_facts import is_subagent_launch_tool
    from reflect.utils import _json_loads

    timestamp = datetime.now(UTC).isoformat()
    identity_keys = (
        "gen_ai.client.tool_name", "gen_ai.tool.name", "ide.tool_name", "tool.name", "tool_name",
        "gen_ai.client.mcp_server", "gen_ai.client.mcp_tool",
        "mcp.server.name", "mcp.server", "server.name", "mcp.tool.name", "mcp.tool",
        "gen_ai.client.hook.event", "ide.hook.event", "event.name", "gen_ai.operation.name",
        "gen_ai.client.agent_id", "gen_ai.memory.id", "gen_ai.request.model", "gen_ai.response.model",
        "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "service.name",
        "reflect.telemetry.origin", "code.module.name",
    )
    projection = ",".join(f"'$.\"{key}\"'" for key in identity_keys)
    changed: dict[str, set[str]] = defaultdict(set)
    launches: list[str] = []
    repaired = removed = 0
    last_rowid = 0
    while rows := conn.execute(
        f"""SELECT tc.rowid,tc.id,tc.step_id,tc.session_id,tc.tool_name,
          json_extract(tc.raw_attrs_json,{projection}),
          COALESCE(json_extract(tc.raw_attrs_json,'$."gen_ai.client.command"'),'')<>''
        FROM tool_calls tc
        WHERE tc.rowid>? ORDER BY tc.rowid LIMIT 256""", (last_rowid,),
    ).fetchall():
        last_rowid = rows[-1][0]
        for _, tool_id, step_id, session_id, old_name, values, has_command in rows:
            attrs = {key: value for key, value in zip(identity_keys, _json_loads(values), strict=True)
                     if value is not None}
            if has_command:
                attrs["gen_ai.client.command"] = True
            # This retained runtime shape was incorrectly projected solely
            # because its span name contained "tool". Other unknowns survive.
            timing = (
                old_name == "codex.tool_blocking"
                and attrs.get("code.module.name") == "codex_core::session::turn"
                and is_codex_otel_service(attrs.get("service.name"))
                and _step_type(str(old_name), attrs) == "unknown"
            )
            if timing:
                conn.execute("DELETE FROM tool_calls WHERE id=?", (tool_id,))
                conn.execute("UPDATE steps SET type='unknown',updated_at=? WHERE id=?", (timestamp, step_id))
                changed[str(session_id)].add(str(step_id))
                removed += 1
                continue
            name = _canonical_tool_name(attrs, old_name)
            # A previous canonical MCP identity may have come from a payload;
            # compact attribute reads must not downgrade that known identity.
            if str(old_name).startswith("mcp__") and not name.startswith("mcp__"):
                name = old_name
            if name != old_name:
                conn.execute("UPDATE tool_calls SET tool_name=?,updated_at=? WHERE id=?", (name, timestamp, tool_id))
                changed[str(session_id)].add(str(step_id))
                repaired += 1
            if is_subagent_launch_tool(name):
                launches.append(str(tool_id))

    repository = HookFactRepository(conn)
    columns = ("tool_name", "raw_attrs_json", "step_id", "session_id", "logical_call_id",
               "input_preview_redacted", "output_preview_redacted")
    promoted = 0
    for offset in range(0, len(launches), 256):
        batch = launches[offset:offset + 256]
        rows = conn.execute(
            f"SELECT {','.join(columns)} FROM tool_calls WHERE id IN ({','.join('?' for _ in batch)})", batch,
        )
        for values in rows:
            tool = dict(zip(columns, values, strict=True))
            if repository.persist_tool_delegation(tool, timestamp=timestamp):
                changed[str(tool["session_id"])].add(str(tool["step_id"]))
                promoted += 1

    # The indexed agent-call population is much smaller than the step history.
    # Inspect only explicit lifecycle event fields, never arbitrary JSON text.
    for step_id, session_id, event, raw_attrs in conn.execute("""
        SELECT id,session_id,summary,raw_attrs_json FROM steps
        WHERE type='agent_call' AND (
          lower(replace(COALESCE(json_extract(raw_attrs_json,'$."gen_ai.client.hook.event"'),
              json_extract(raw_attrs_json,'$."ide.hook.event"'),
              json_extract(raw_attrs_json,'$."event.name"'),summary,''),'_','')) GLOB '*subagentstart'
          OR lower(replace(COALESCE(json_extract(raw_attrs_json,'$."gen_ai.client.hook.event"'),
              json_extract(raw_attrs_json,'$."ide.hook.event"'),
              json_extract(raw_attrs_json,'$."event.name"'),summary,''),'_','')) GLOB '*subagentstop'
        )
    """):
        repository.persist_step(step_id=step_id, session_id=session_id,
                                fallback_event=event or "", attrs=_json_loads(raw_attrs), timestamp=timestamp)
        changed[str(session_id)].add(str(step_id))
        promoted += 1
    commands = refresh_command_projection(conn)
    pending = PendingRefreshState(conn)
    for session_id, steps in changed.items():
        pending.mark_steps(session_id, steps, phases=("rollup", "graph", "improvements"))
    return {"tool_names": repaired, "timing_projections_removed": removed,
            "delegation_facts": promoted, "command_projections": commands}


def _classify_retained_event_spans(conn: sqlite3.Connection) -> int:
    """Migration 51 changes only proven non-operation categories, in bounded batches.

    Historical rows that now suggest a tool/model call remain unclassified:
    changing their type alone would invent a projection that was never stored.
    Raw evidence, operation tables, accounting and execution work are untouched.
    """
    from reflect.store.normalize import NON_OPERATION_SPAN_TYPES, _load_json, _step_type

    timestamp = datetime.now(UTC).isoformat()
    last_rowid = changed = 0
    while rows := conn.execute(
        "SELECT rowid,id,summary,raw_attrs_json FROM steps "
        "WHERE rowid>? AND type='unknown' ORDER BY rowid LIMIT 256", (last_rowid,),
    ).fetchall():
        last_rowid = rows[-1][0]
        updates = []
        for _, step_id, event, raw_attrs in rows:
            category = _step_type(event or "", _load_json(raw_attrs))
            if category in NON_OPERATION_SPAN_TYPES:
                updates.append((category, timestamp, step_id))
        conn.executemany("UPDATE steps SET type=?,updated_at=? WHERE id=?", updates)
        changed += len(updates)
    return changed


def _repair_codex_span_identity(conn: sqlite3.Connection) -> dict[str, int]:
    """Collapse exact Codex dispatch copies and restore their canonical calls."""
    from collections import defaultdict

    from reflect.store.normalize import (
        _first_text,
        _insert_call_record,
        _load_json,
        _stable_id,
        _step_type,
    )
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.telemetry_facts import recorded_tool_name

    timestamp = datetime.now(UTC).isoformat()
    call_reference_tables: list[tuple[str, str]] = []
    for table_row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        table = table_row[0]
        for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
            if foreign_key[2] == "tool_calls":
                call_reference_tables.append((table, foreign_key[3]))
    groups: dict[tuple[str, str, str, str], list[str]] = defaultdict(list)
    for event_id, session_id, trace_id, span_id, content_hash in conn.execute(
        """SELECT id,session_id,trace_id,span_id,content_hash FROM raw_events
        WHERE source_type='otlp_traces_json' AND trace_id<>'' AND span_id<>''
          AND COALESCE(json_extract(attrs_json,'$.\"service.name\"'),'') IN
              ('codex-app-server','codex-exec-server','codex_exec')
          AND json_extract(attrs_json,'$.\"code.module.name\"')='codex_core::tools::parallel'
          AND json_extract(attrs_json,'$.\"codex.tool.phase\"')='dispatch'
          AND COALESCE(json_extract(attrs_json,'$.\"call_id\"'),'')<>''
          AND COALESCE(session_id,'')<>''"""
    ):
        groups[(session_id, trace_id, span_id, content_hash)].append(event_id)

    pairs = []
    for (session_id, _, _, _), event_ids in groups.items():
        if len(event_ids) < 2:
            continue
        step_ids = sorted(_stable_id("step", event_id) for event_id in event_ids)
        keeper = step_ids[0]
        pairs.extend((loser, keeper, session_id) for loser in step_ids[1:])

    removed_steps = 0
    if pairs:
        conn.execute("CREATE TEMP TABLE codex_span_duplicates(loser TEXT PRIMARY KEY, keeper TEXT NOT NULL, session_id TEXT NOT NULL)")
        conn.executemany("INSERT INTO codex_span_duplicates VALUES (?,?,?)", pairs)
        unsafe = set()
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            for foreign_key in conn.execute(f'PRAGMA foreign_key_list("{table}")'):
                if foreign_key[2] != "steps" or table == "execution_unit_steps":
                    continue
                column = foreign_key[3]
                unsafe.update(row[0] for row in conn.execute(
                    f"SELECT d.loser FROM codex_span_duplicates d JOIN \"{table}\" c ON c.\"{column}\"=d.loser"
                ))
        for loser, keeper in conn.execute("SELECT loser,keeper FROM codex_span_duplicates"):
            keeper_units = {r[0] for r in conn.execute("SELECT execution_unit_id FROM execution_unit_steps WHERE step_id=?", (keeper,))}
            loser_units = {r[0] for r in conn.execute("SELECT execution_unit_id FROM execution_unit_steps WHERE step_id=?", (loser,))}
            if loser in unsafe or keeper_units != loser_units:
                unsafe.add(loser)
        safe = [loser for loser, _, _ in pairs if loser not in unsafe]
        conn.executemany("DELETE FROM steps WHERE id=?", ((step_id,) for step_id in safe))
        removed_steps = len(safe)
        conn.execute("DROP TABLE codex_span_duplicates")

    changed: dict[str, set[str]] = defaultdict(set)
    classified = 0
    rows = conn.execute(
        """SELECT id,source_id,source_type,event_type,session_id,observed_at,received_at,
                  origin_kind,attrs_json,content_hash
        FROM raw_events WHERE source_type='otlp_traces_json'
          AND json_extract(attrs_json,'$.\"code.module.name\"')='codex_core::tools::parallel'
          AND json_extract(attrs_json,'$.\"codex.tool.phase\"')='dispatch'
          AND COALESCE(json_extract(attrs_json,'$.\"call_id\"'),'')<>''
          AND COALESCE(session_id,'')<>'' ORDER BY id"""
    )
    for raw in rows:
        event = dict(zip(("id","source_id","source_type","event_type","session_id","observed_at","received_at","origin_kind","attrs_json","content_hash"), raw, strict=True))
        attrs = _load_json(event["attrs_json"])
        step_id = _stable_id("step", event["id"])
        step = conn.execute("SELECT type,summary FROM steps WHERE id=?", (step_id,)).fetchone()
        if not step:
            continue
        category = _step_type(str(step[1] or event["event_type"]), attrs)
        if category != "tool_call":
            continue

        call_id = str(attrs["call_id"]).strip()
        turn_id = _first_text(attrs, "turn.id", "turn_id")
        dispatch_name = recorded_tool_name(attrs)
        own_call = conn.execute(
            """SELECT id,logical_call_id,tool_name,status,input_hash,output_hash,
                      input_preview_redacted,output_preview_redacted,error_type,error_message_redacted,
                      raw_attrs_json
               FROM tool_calls WHERE step_id=? AND session_id=?""",
            (step_id, event["session_id"]),
        ).fetchone()
        canonical_call = conn.execute(
            """SELECT id,tool_name,tool_type,raw_attrs_json FROM tool_calls
               WHERE session_id=? AND logical_call_id=?""",
            (event["session_id"], call_id),
        ).fetchone()
        if own_call and own_call[1] != call_id:
            own_attrs = _load_json(own_call[10])
            own_turn = _first_text(own_attrs, "turn.id", "turn_id")
            own_name = recorded_tool_name(own_attrs) or own_call[2]
            own_is_empty_dispatch = (
                own_call[3] == "unknown"
                and not any(own_call[index] for index in range(4, 10))
            )
            if canonical_call:
                canonical_attrs = _load_json(canonical_call[3])
                canonical_turn = _first_text(canonical_attrs, "turn.id", "turn_id")
                canonical_name = recorded_tool_name(canonical_attrs) or canonical_call[1]
                same_turn = bool(turn_id and canonical_turn == turn_id)
                same_name = (
                    canonical_name == dispatch_name
                    or canonical_call[2] == "mcp"
                )
                dependents = []
                for table, column in call_reference_tables:
                    if conn.execute(
                        f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1',
                        (own_call[0],),
                    ).fetchone():
                        dependents.append(table)
                if not (
                    same_turn and own_turn == turn_id and same_name
                    and own_is_empty_dispatch and not dependents
                ):
                    # Ambiguous collisions remain visible as raw evidence and
                    # are not promoted into another canonical invocation.
                    continue
                conn.execute("DELETE FROM tool_calls WHERE id=?", (own_call[0],))
            elif own_turn == turn_id and own_name == dispatch_name:
                conn.execute(
                    "UPDATE tool_calls SET logical_call_id=?,updated_at=? WHERE id=?",
                    (call_id, timestamp, own_call[0]),
                )
            else:
                continue
        if step[0] != "tool_call":
            conn.execute("UPDATE steps SET type='tool_call',updated_at=? WHERE id=?", (timestamp, step_id))
        _insert_call_record(conn, raw_event=event, attrs=attrs, session_id=event["session_id"],
                            step_id=step_id, step_type="tool_call", timestamp=timestamp)
        changed[event["session_id"]].add(step_id)
        classified += int(step[0] != "tool_call")

    pending = PendingRefreshState(conn)
    for session_id, step_ids in changed.items():
        pending.mark_steps(session_id, step_ids, phases=("rollup", "graph", "improvements"))
    return {"duplicate_steps_removed": removed_steps, "dispatch_steps_classified": classified}


def migrate(conn: sqlite3.Connection, *, commit: bool = True) -> list[int]:
    """Apply pending migrations, optionally inside a caller-owned transaction."""
    from reflect.improvements.workflow_identity import workflow_identity_from_json
    from reflect.store.provenance import telemetry_agent_name, unassigned_codex_classification
    from reflect.telemetry_facts import TURN_TERMINAL_KEYS, event_time, observed_turn_terminal
    from reflect.tool_outcomes import tool_failure
    from reflect.utils import _json_dumps, _json_loads

    def failure_json(event: str, raw_attrs: str) -> str | None:
        try:
            attrs = _json_loads(raw_attrs)
        except (ValueError, TypeError):
            return None
        if not isinstance(attrs, dict):
            return None
        failure = tool_failure(event, attrs)
        return _json_dumps({"type": failure.error_type, "message": failure.message}) if failure else None

    conn.create_function("reflect_tool_failure", 2, failure_json, deterministic=True)
    conn.create_function(
        "reflect_observed_turn_terminal", 1,
        lambda values: int(observed_turn_terminal(dict(zip(TURN_TERMINAL_KEYS, _json_loads(values), strict=True)))),
        deterministic=True,
    )
    conn.create_function(
        "reflect_event_time", 1,
        lambda value: at.isoformat(timespec="microseconds") if (at := event_time(value)) else None,
        deterministic=True,
    )
    conn.create_function(
        "reflect_agent_name", 1,
        lambda name: telemetry_agent_name({"gen_ai.client.name": name}), deterministic=True,
    )
    conn.create_function(
        "reflect_unassigned_codex", 1,
        lambda attrs: unassigned_codex_classification(_json_loads(attrs)), deterministic=True,
    )
    conn.create_function(
        "reflect_stable_id", 2,
        lambda prefix, value: f"{prefix}_{hashlib.sha1(str(value or '').encode()).hexdigest()}",
        deterministic=True,
    )

    conn.create_function(
        "reflect_workflow_contract_signature",
        2,
        lambda title, content: workflow_identity_from_json(
            title, content
        ).contract_signature,
        deterministic=True,
    )
    conn.create_function(
        "reflect_workflow_revision_hash",
        2,
        lambda title, content: workflow_identity_from_json(title, content).revision_hash,
        deterministic=True,
    )
    migrations = load_migrations()
    try:
        applied_fast = {
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        }
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        applied_fast = set()
    if all(migration.version in applied_fast for migration in migrations):
        return []
    savepoint = "reflect_schema_migrate"
    if commit:
        if conn.in_transaction:
            conn.commit()
        conn.execute("BEGIN IMMEDIATE")
    elif not conn.in_transaction:
        raise ValueError("commit=False requires a caller-owned transaction")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    applied_now: list[int] = []
    try:
        applied = applied_migration_versions(conn)
        for migration in migrations:
            if migration.version in applied:
                continue
            for statement in _migration_statements(migration.sql):
                conn.execute(statement)
            if migration.version == 50:
                _repair_tool_evidence(conn)
            if migration.version == 51:
                _classify_retained_event_spans(conn)
            if migration.version == 52:
                _repair_codex_span_identity(conn)
            conn.execute(
                "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, datetime.now(UTC).isoformat()),
            )
            applied_now.append(migration.version)
        if commit:
            conn.commit()
        else:
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        return applied_now
    except Exception:
        if commit:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise
