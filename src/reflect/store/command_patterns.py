"""Persist command projections after canonical input/result evidence is merged."""
from __future__ import annotations

import sqlite3

from reflect.command_patterns import COMMAND_ATTRIBUTES, STRUCTURED_SHELL_TOOLS, command_projection

_COMMAND_SQL = "COALESCE(" + ",".join(
    f"NULLIF(json_extract(raw_attrs_json, '$.\"{key}\"'), '')" for key in COMMAND_ATTRIBUTES
) + ")"
_NAMES_SQL = ",".join("'" + name + "'" for name in sorted(STRUCTURED_SHELL_TOOLS))
_NAMESPACED_SQL = " OR ".join(
    f"LOWER(tool_name) GLOB '*.{name}'" for name in sorted(STRUCTURED_SHELL_TOOLS)
)
_CANDIDATE_SQL = f"(LOWER(tool_name) IN ({_NAMES_SQL}) OR {_NAMESPACED_SQL} OR {_COMMAND_SQL} IS NOT NULL)"
_ATTRS_SQL = "json_object(" + ",".join(
    f"'{key}',json_extract(raw_attrs_json, '$.\"{key}\"')"
    for key in (*COMMAND_ATTRIBUTES, "gen_ai.client.tool.input", "tool.input")
) + ")"


def refresh_command_projection(conn: sqlite3.Connection, tool_call_id: str | None = None) -> int:
    """Refresh changed values only; the caller owns the transaction."""
    changed = 0
    last_id = None
    while True:
        clause, params = ("id = ?", (tool_call_id,)) if tool_call_id is not None else (
            ("id > ?", (last_id,)) if last_id is not None else ("1 = 1", ())
        )
        # Only command/input attributes cross into Python, never tool output.
        # Keyset batches are materialized before UPDATEs to avoid mutating an
        # active scan and to bound memory during the one-time migration.
        rows = conn.execute(
            f"""SELECT id, tool_name,
            CASE WHEN {_CANDIDATE_SQL} THEN {_ATTRS_SQL} ELSE '{{}}' END,
            CASE WHEN {_CANDIDATE_SQL} THEN input_preview_redacted END,
            command_template, is_shell_call
            FROM tool_calls WHERE {clause} ORDER BY id LIMIT 128""", params,
        ).fetchall()
        if not rows:
            break
        updates = []
        for call_id, tool_name, attrs, preview, old_template, old_shell in rows:
            template, is_shell = command_projection(tool_name, attrs, preview)
            if (template, int(is_shell)) != (old_template, old_shell):
                updates.append((template, int(is_shell), call_id))
        conn.executemany(
            "UPDATE tool_calls SET command_template = ?, is_shell_call = ? WHERE id = ?", updates,
        )
        changed += len(updates)
        if tool_call_id is not None:
            break
        last_id = rows[-1][0]
    return changed
