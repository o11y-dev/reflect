from __future__ import annotations

import re
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from reflect.parsing import (
    _discover_rich_session_files,
    _infer_otlp_logs_file,
    _native_session_path_matches_id,
)

if TYPE_CHECKING:
    from reflect.preparation import PreparationProgressReporter

def prepare_usage_db(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool,
    native_session_ids: tuple[str, ...] = (),
    progress: PreparationProgressReporter | None = None,
    keep_processed_raw: bool = False,
) -> dict[str, object]:
    """Refresh usage facts without rebuilding graph or improvement state."""
    from reflect.preparation import PreparationLock, PreparationStage, report_preparation_progress
    from reflect.store.cursor_usage import (
        apply_cursor_transcript_usage_estimates,
        repair_misattributed_cursor_transcript_usage,
    )
    from reflect.store.ingest import (
        SegmentBatchIngestion,
        finalize_processed_segments,
        ingest_codex_context_file,
        ingest_native_session_file,
        ingest_otlp_log_segments,
        ingest_otlp_trace_segments,
    )
    from reflect.store.migrate import migrate
    from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
    from reflect.store.rollups import rebuild_rollups, refresh_rollups, rollup_rebuild_pending
    from reflect.store.sqlite import connect_sqlite, mark_snapshot, snapshot_incomplete
    from reflect.store.workspaces import backfill_session_context

    report_preparation_progress(
        progress,
        PreparationStage.WAITING_FOR_STORE,
        "Waiting for any active refresh of this store...",
    )
    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        report_preparation_progress(
            progress,
            PreparationStage.OPENING_STORE,
            "Opening the local telemetry store...",
        )
        report_preparation_progress(
            progress,
            PreparationStage.MIGRATING_SCHEMA,
            "Checking database migrations...",
        )
        applied = migrate(conn)
        recovering = snapshot_incomplete(conn, "usage")
        report_was_ready = not snapshot_incomplete(conn)
        mark_snapshot(conn, ready=False)
        segment_batches: list[SegmentBatchIngestion] = []
        if otlp_traces is not None:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new OTLP traces...",
            )
            segment_batches.append(
                ingest_otlp_trace_segments(conn, active_path=otlp_traces)
            )
            otlp_logs = _infer_otlp_logs_file(otlp_traces)
            if otlp_logs is not None:
                report_preparation_progress(
                    progress,
                    PreparationStage.INGESTING_LOGS,
                    "Reading new OTLP logs...",
                )
                segment_batches.append(
                    ingest_otlp_log_segments(conn, active_path=otlp_logs)
                )

        changed_session_ids: set[str] = set()
        normalization_failed = 0
        if conn.execute(
            "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
        ).fetchone():
            report_preparation_progress(
                progress,
                PreparationStage.NORMALIZING,
                "Normalizing usage telemetry...",
            )
            normalize_result = normalize_pending_raw_events(
                conn,
                changed_session_ids=changed_session_ids,
            )
            normalization_failed += int(normalize_result.get("failed", 0))

        cursor_native_files: list[Path] = []
        if include_native_sessions:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_SESSIONS,
                "Reading local agent sessions...",
            )
            for native_agent, session_file in _discover_rich_session_files():
                if native_session_ids and native_agent != "opencode" and not any(
                    _native_session_path_matches_id(session_file, candidate)
                    for candidate in native_session_ids
                ):
                    continue
                if native_agent == "codex":
                    ingest_codex_context_file(conn, file_path=session_file)
                result = ingest_native_session_file(
                    conn,
                    file_path=session_file,
                    agent=native_agent,
                    source_id=f"native_session:{native_agent}:{session_file}",
                    skip_existing_sessions=native_agent != "opencode",
                    skip_unchanged=True,
                )
                if native_agent == "cursor" and not result.get("unchanged"):
                    cursor_native_files.append(session_file)
            report_preparation_progress(
                progress,
                PreparationStage.NORMALIZING,
                "Normalizing usage telemetry...",
            )
            normalize_result = normalize_pending_raw_events(
                conn,
                changed_session_ids=changed_session_ids,
            )
            normalization_failed += int(normalize_result.get("failed", 0))
        report_preparation_progress(
            progress,
            PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating canonical usage state...",
        )
        backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
            changed_session_ids=changed_session_ids,
        )
        cursor_provenance_result = repair_misattributed_cursor_transcript_usage(conn)
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_provenance_result["session_ids"]
            if session_id
        )
        cursor_result = apply_cursor_transcript_usage_estimates(conn, cursor_native_files)
        context_result = backfill_session_context(
            conn,
            timestamp=datetime.now(UTC).isoformat(),
            changed_session_ids=changed_session_ids,
        )
        session_count = int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])
        rollup_count = int(conn.execute("SELECT COUNT(*) FROM session_rollups").fetchone()[0])
        from reflect.store.cost_refresh import CostRefreshState

        cost_state = CostRefreshState(conn)
        cost_inputs = cost_state.inspect(session_ids=changed_session_ids) if session_count else None
        rebuild_usage = (
            recovering
            or rollup_rebuild_pending(conn)
            or session_count != rollup_count
            or cursor_result.get("updated")
            or (cost_inputs is not None and cost_inputs.requires_full_refresh)
        )
        if rebuild_usage:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                "Rebuilding usage rollups...",
            )
            ensure_sql_costs(conn, refresh_inputs=cost_inputs)
            rebuild_rollups(conn)
            if cost_inputs is not None and cost_inputs.requires_full_refresh:
                cost_state.mark_current(cost_inputs)
        elif changed_session_ids or context_result["sessions_updated"]:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Refreshing usage rollups for {len(changed_session_ids):,} session(s)...",
            )
            ensure_sql_costs(
                conn,
                session_ids=changed_session_ids,
                refresh_inputs=cost_inputs,
            )
            refresh_rollups(conn, changed_session_ids)
        raw_cleanup_result = (
            finalize_processed_segments(
                conn,
                segment_batches,
                keep_processed_raw=keep_processed_raw,
            )
            if not normalization_failed
            else {
                "deleted_segments": 0,
                "deleted_bytes": 0,
                "retained_segments": sum(
                    len(batch.processed_closed_paths) for batch in segment_batches
                ),
            }
        )
        report_preparation_progress(
            progress,
            PreparationStage.COMPLETE,
            "Usage refresh complete.",
        )
        if not conn.execute(
            "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
        ).fetchone():
            # A successful no-change usage refresh preserves the prior report.
            # Changed or interrupted runs still require full report derivation.
            report_unchanged = (
                report_was_ready and not applied and not rebuild_usage
                and not changed_session_ids and not context_result["sessions_updated"]
            )
            mark_snapshot(conn, ready=True, profile="snapshot" if report_unchanged else "usage")
        return {
            "refreshed": True,
            "changed_sessions": len(changed_session_ids),
            "raw_cleanup": raw_cleanup_result,
        }


def ensure_sql_costs(
    conn,
    *,
    alias_path: Path | None = None,
    session_ids: set[str] | None = None,
    refresh_inputs=None,
):
    from reflect.config import load_model_aliases
    from reflect.cost_aliases import ensure_cost_aliases
    from reflect.pricing import load_pricing_table

    if refresh_inputs is None:
        pricing_table = load_pricing_table()
        alias_result = ensure_cost_aliases(
            conn,
            alias_path=alias_path,
            pricing_table=pricing_table,
            session_ids=session_ids,
        )
        aliases = load_model_aliases(alias_result.alias_path)
    else:
        pricing_table = refresh_inputs.pricing_table
        alias_result = refresh_inputs.alias_result
        aliases = refresh_inputs.aliases
        if refresh_inputs.requires_full_refresh:
            session_ids = None
    reprice_sql_store(
        conn,
        alias_path=alias_result.alias_path,
        session_ids=session_ids,
        pricing_table=pricing_table,
        aliases=aliases,
    )
    return alias_result


def cursor_native_parent_session_id(source_ref: str) -> str:
    match = re.search(r"/agent-transcripts/([^/]+)/", source_ref or "")
    return match.group(1) if match else ""


def prepare_sql_report_db(
    db_path: Path,
    *,
    otlp_traces: Path | None,
    include_native_sessions: bool = False,
    spans_dir: Path | None = None,
    progress: PreparationProgressReporter | None = None,
    defer_otlp_replay: bool = False,
    keep_processed_raw: bool = False,
) -> dict[str, object]:
    from reflect.preparation import PreparationLock, PreparationStage, report_preparation_progress
    from reflect.store.cost_refresh import CostRefreshState
    from reflect.store.cursor_usage import (
        apply_cursor_transcript_usage_estimates,
        repair_misattributed_cursor_transcript_usage,
    )
    from reflect.store.graph_normalize import rebuild_graph, refresh_graph
    from reflect.store.ingest import (
        AppendOnlyReplayPolicy,
        SegmentBatchIngestion,
        finalize_processed_segments,
        ingest_codex_context_file,
        ingest_local_spans_file,
        ingest_native_session_file,
        ingest_otlp_log_segments,
        ingest_otlp_trace_segments,
    )
    from reflect.store.migrate import migrate
    from reflect.store.normalize import (
        backfill_mcp_calls,
        backfill_tool_call_hashes,
        normalize_pending_raw_events,
    )
    from reflect.store.refresh_plan import RefreshMode, plan_derived_refresh
    from reflect.store.rollups import rebuild_rollups, refresh_rollups, rollup_rebuild_pending
    from reflect.store.sqlite import connect_sqlite, mark_snapshot, snapshot_incomplete
    from reflect.store.workspaces import backfill_session_context

    report_preparation_progress(
        progress,
        PreparationStage.WAITING_FOR_STORE,
        "Waiting for any active refresh of this store...",
    )
    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        report_preparation_progress(
            progress,
            PreparationStage.OPENING_STORE,
            "Opening the local telemetry store...",
        )
        report_preparation_progress(
            progress,
            PreparationStage.MIGRATING_SCHEMA,
            "Checking database migrations...",
        )
        applied = migrate(conn)
        recovering = snapshot_incomplete(conn)
        mark_snapshot(conn, ready=False)
        ingest_result = {"inserted": 0, "skipped": 0}
        ingest_sources: dict[str, dict[str, object]] = {}
        segment_batches: list[SegmentBatchIngestion] = []
        cursor_native_files: list[Path] = []
        replay_policy = (
            AppendOnlyReplayPolicy.DEFER
            if defer_otlp_replay
            else AppendOnlyReplayPolicy.REPLAY
        )
        if otlp_traces is not None:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new OTLP traces...",
            )
            traces_batch = ingest_otlp_trace_segments(
                conn,
                active_path=otlp_traces,
                replay_policy=replay_policy,
            )
            segment_batches.append(traces_batch)
            ingest_sources["otlp_traces"] = traces_batch.as_dict()
            ingest_result["inserted"] += traces_batch.inserted
            ingest_result["skipped"] += traces_batch.skipped
            otlp_logs = _infer_otlp_logs_file(otlp_traces)
            if otlp_logs is not None:
                report_preparation_progress(
                    progress,
                    PreparationStage.INGESTING_LOGS,
                    "Reading new OTLP logs...",
                )
                logs_batch = ingest_otlp_log_segments(
                    conn,
                    active_path=otlp_logs,
                    replay_policy=replay_policy,
                )
                segment_batches.append(logs_batch)
                ingest_sources["otlp_logs"] = logs_batch.as_dict()
                ingest_result["inserted"] += logs_batch.inserted
                ingest_result["skipped"] += logs_batch.skipped
        if spans_dir is not None and spans_dir.is_dir():
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_TRACES,
                "Reading new hook spans...",
            )
            spans_result: dict[str, object] = {
                "inserted": 0,
                "skipped": 0,
                "unchanged": 0,
                "files": 0,
                "source_type": "local_spans_jsonl",
            }
            for span_file in sorted(spans_dir.glob("*.jsonl")):
                file_result = ingest_local_spans_file(
                    conn,
                    file_path=span_file,
                    skip_unchanged=True,
                )
                spans_result["files"] = int(spans_result["files"]) + 1
                for key in ("inserted", "skipped", "unchanged"):
                    spans_result[key] = int(spans_result[key]) + int(
                        file_result.get(key, 0)
                    )
            ingest_sources["local_spans"] = spans_result
            ingest_result["inserted"] += int(spans_result["inserted"])
            ingest_result["skipped"] += int(spans_result["skipped"])
        if include_native_sessions:
            report_preparation_progress(
                progress,
                PreparationStage.INGESTING_SESSIONS,
                "Reading local agent sessions...",
            )
            native_result = {"inserted": 0, "skipped": 0, "unchanged": 0}
            context_result = {"inserted": 0, "skipped": 0, "unchanged": 0}
            for agent, session_file in _discover_rich_session_files():
                source_ref = f"native_session:{agent}:{session_file}"
                if agent == "codex":
                    context_file_result = ingest_codex_context_file(
                        conn,
                        file_path=session_file,
                    )
                    for key in context_result:
                        context_result[key] += int(context_file_result.get(key, 0))
                result = ingest_native_session_file(
                    conn,
                    file_path=session_file,
                    agent=agent,
                    source_id=source_ref,
                    skip_existing_sessions=agent != "opencode",
                    skip_unchanged=True,
                )
                native_result["inserted"] += result["inserted"]
                native_result["skipped"] += result["skipped"]
                native_result["unchanged"] += result.get("unchanged", 0)
                if agent == "cursor" and not result.get("unchanged"):
                    cursor_native_files.append(session_file)
            if any(native_result.values()):
                ingest_sources["native_sessions"] = native_result
                ingest_sources["native_sessions"]["source_type"] = "native_session"
                ingest_result["inserted"] += native_result["inserted"]
                ingest_result["skipped"] += native_result["skipped"]
            if any(context_result.values()):
                ingest_sources["context_artifacts"] = context_result
                ingest_sources["context_artifacts"]["source_type"] = "native_context"
                ingest_result["inserted"] += context_result["inserted"]
                ingest_result["skipped"] += context_result["skipped"]
        needs_normalize = bool(
            ingest_result["inserted"]
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE origin_kind IS NULL LIMIT 1"
            ).fetchone()
        )
        changed_session_ids: set[str] = set()
        report_preparation_progress(
            progress,
            PreparationStage.NORMALIZING,
            "Normalizing new telemetry...",
        )
        normalize_result = (
            normalize_pending_raw_events(
                conn,
                changed_session_ids=changed_session_ids,
            )
            if needs_normalize
            else {"processed": 0, "failed": 0, "skipped": 0}
        )
        report_preparation_progress(
            progress,
            PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating canonical session state...",
        )
        fingerprint_result = backfill_tool_call_hashes(conn)
        mcp_backfill_result = backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
            changed_session_ids=changed_session_ids,
        )
        cursor_provenance_result = repair_misattributed_cursor_transcript_usage(conn)
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_provenance_result["session_ids"]
            if session_id
        )
        context_result = backfill_session_context(
            conn,
            timestamp=datetime.now(UTC).isoformat(),
            changed_session_ids=changed_session_ids,
        )
        cursor_usage_result = apply_cursor_transcript_usage_estimates(
            conn,
            cursor_native_files,
        )
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_usage_result.get("session_ids", [])
            if session_id
        )
        reconciled_legacy_data = rollup_rebuild_pending(conn)
        all_session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
        if recovering:
            # A prior process may have committed normalization before its derived
            # work. Reconcile every session even when no raw events remain pending.
            changed_session_ids.update(all_session_ids)
        rollup_session_ids = {
            str(row[0]) for row in conn.execute("SELECT session_id FROM session_rollups")
        }
        graph_session_ids = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT session_id FROM graph_nodes "
                "WHERE kind = 'Session' AND session_id IS NOT NULL"
            )
        }
        cost_state = CostRefreshState(conn)
        cost_inputs = (
            cost_state.inspect(session_ids=changed_session_ids)
            if all_session_ids
            else None
        )
        refresh_plan = plan_derived_refresh(
            changed_session_ids=changed_session_ids,
            all_session_ids=all_session_ids,
            graph_session_ids=graph_session_ids,
            rollup_session_ids=rollup_session_ids,
            graph_exists=bool(graph_session_ids),
            force_full_cost_reason=(
                cost_inputs.reason
                if cost_inputs is not None and cost_inputs.requires_full_refresh
                else ""
            ),
            force_full_rollup_reason=(
                "Telemetry migration requires reconciliation"
                if reconciled_legacy_data
                else ""
            ),
        )

        if refresh_plan.cost_mode is RefreshMode.FULL:
            ensure_sql_costs(conn, refresh_inputs=cost_inputs)
        elif refresh_plan.cost_mode is RefreshMode.INCREMENTAL:
            ensure_sql_costs(
                conn,
                session_ids=set(refresh_plan.cost_session_ids),
                refresh_inputs=cost_inputs,
            )

        if refresh_plan.graph_mode is RefreshMode.FULL:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_GRAPH,
                f"Rebuilding the evidence graph ({refresh_plan.graph_reason})...",
            )
            graph_result = rebuild_graph(conn)
        elif refresh_plan.graph_mode is RefreshMode.INCREMENTAL:
            graph_targets = set(refresh_plan.graph_session_ids)
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_GRAPH,
                f"Refreshing the evidence graph for {len(graph_targets):,} session(s)...",
            )
            graph_result = refresh_graph(conn, graph_targets)
        else:
            graph_result = {
                "nodes": 0,
                "edges": 0,
                "skipped": 1,
                "reason": refresh_plan.graph_reason,
            }

        if refresh_plan.rollup_mode is RefreshMode.FULL:
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Rebuilding session rollups ({refresh_plan.rollup_reason})...",
            )
            rollup_result = rebuild_rollups(conn)
        elif refresh_plan.rollup_mode is RefreshMode.INCREMENTAL:
            rollup_targets = set(refresh_plan.rollup_session_ids)
            report_preparation_progress(
                progress,
                PreparationStage.REFRESHING_ROLLUPS,
                f"Refreshing rollups for {len(rollup_targets):,} session(s)...",
            )
            rollup_result = refresh_rollups(conn, rollup_targets)
        else:
            rollup_result = {
                "session_rollups": len(rollup_session_ids),
                "daily_rollups": int(conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0]),
                "tool_rollups": int(conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0]),
                "skipped": 1,
                "reason": refresh_plan.rollup_reason,
            }
        if cost_inputs is not None and cost_inputs.requires_full_refresh:
            cost_state.mark_current(cost_inputs)
        from reflect.improvements.service import ImprovementService

        report_preparation_progress(
            progress,
            PreparationStage.REFRESHING_IMPROVEMENTS,
            "Refreshing evidence-backed improvements...",
        )
        improvement_result = ImprovementService(conn).refresh()
        raw_cleanup_result = (
            finalize_processed_segments(
                conn,
                segment_batches,
                keep_processed_raw=keep_processed_raw,
            )
            if not int(normalize_result.get("failed", 0))
            else {
                "deleted_segments": 0,
                "deleted_bytes": 0,
                "retained_segments": sum(
                    len(batch.processed_closed_paths) for batch in segment_batches
                ),
            }
        )
        refresh_completed_at = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO store_metadata(key, value, updated_at)
            VALUES ('last_successful_refresh', ?, ?)
            ON CONFLICT(key) DO UPDATE SET
              value = excluded.value,
              updated_at = excluded.updated_at
            """,
            (refresh_completed_at, refresh_completed_at),
        )
        conn.commit()
        if not conn.execute(
            "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
        ).fetchone():
            mark_snapshot(conn, ready=True)
    deferred_replays = [
        source
        for source_result in ingest_sources.values()
        for source, result in dict(source_result.get("sources") or {}).items()
        if isinstance(result, dict) and result.get("mode") == "replay_deferred"
    ]
    result = {
        "sessions": len(all_session_ids),
        "applied_migrations": applied,
        "ingest": ingest_result,
        "ingest_sources": ingest_sources,
        "normalize": normalize_result,
        "mcp_calls": mcp_backfill_result,
        "tool_call_fingerprints": fingerprint_result,
        "session_context": context_result,
        "cursor_provenance": cursor_provenance_result,
        "cursor_usage_estimates": cursor_usage_result,
        "refresh_plan": refresh_plan.as_dict(),
        "graph": graph_result,
        "rollups": rollup_result,
        "improvements": improvement_result,
        "raw_cleanup": raw_cleanup_result,
        "deferred_replays": deferred_replays,
    }
    report_preparation_progress(
        progress,
        PreparationStage.COMPLETE,
        "Preparation complete.",
    )
    return result


def reprice_sql_store(
    conn,
    *,
    alias_path: Path | None = None,
    session_ids: set[str] | None = None,
    pricing_table=None,
    aliases: dict[str, str] | None = None,
) -> None:
    from reflect.config import load_model_aliases
    from reflect.pricing import calculate_cost, load_pricing_table

    pricing_table = pricing_table or load_pricing_table()
    if aliases is None:
        aliases = load_model_aliases(alias_path)
    import sqlite3

    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        scoped_ids = sorted(session_ids) if session_ids is not None else None
        if scoped_ids is not None and not scoped_ids:
            return
        placeholders = ", ".join("?" for _ in scoped_ids or [])
        llm_scope = f"WHERE session_id IN ({placeholders})" if scoped_ids is not None else ""
        selected_session_scope = f"WHERE id IN ({placeholders})" if scoped_ids is not None else ""
        selected_session_rows = conn.execute(
            f"SELECT id, source_ref FROM sessions {selected_session_scope}",
            scoped_ids or [],
        ).fetchall()
        model_scope_ids = set(scoped_ids or [])
        for selected_session in selected_session_rows:
            parent_id = cursor_native_parent_session_id(str(selected_session["source_ref"] or ""))
            if parent_id:
                model_scope_ids.add(parent_id)
        model_scope_values = sorted(model_scope_ids)
        model_placeholders = ", ".join("?" for _ in model_scope_values)
        model_scope = (
            f"AND session_id IN ({model_placeholders})"
            if scoped_ids is not None
            else ""
        )
        rows = conn.execute(
            f"""
            SELECT
              id,
              session_id,
              COALESCE(NULLIF(response_model, ''), NULLIF(request_model, '')) AS model,
              input_tokens,
              output_tokens,
              cache_creation_input_tokens,
              cache_read_input_tokens,
              reasoning_output_tokens
            FROM llm_calls
            {llm_scope}
            """,
            scoped_ids or [],
        ).fetchall()
        model_rows = conn.execute(
            f"""
            SELECT
              session_id,
              COALESCE(
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
              ) AS model,
              COUNT(*) AS count
            FROM steps
            WHERE COALESCE(
              NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
              NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
            ) IS NOT NULL
              {model_scope}
            GROUP BY session_id, model
            ORDER BY session_id ASC, count DESC
            """,
            model_scope_values if scoped_ids is not None else [],
        ).fetchall()
        session_models: dict[str, str] = {}
        for model_row in model_rows:
            session_models.setdefault(model_row["session_id"], model_row["model"])
        seen_usage: set[tuple] = set()
        session_costs: dict[str, float] = {}
        session_tokens: dict[str, dict[str, int]] = {}
        session_model_hints: dict[str, str] = {}
        for row in rows:
            model = row["model"] or session_models.get(row["session_id"], "")
            if model:
                session_model_hints.setdefault(row["session_id"], model)
            usage_key = (
                row["session_id"],
                model,
                int(row["input_tokens"] or 0),
                int(row["output_tokens"] or 0),
                int(row["cache_creation_input_tokens"] or 0),
                int(row["cache_read_input_tokens"] or 0),
                int(row["reasoning_output_tokens"] or 0),
            )
            breakdown = calculate_cost(
                {
                    "input": row["input_tokens"],
                    "output": row["output_tokens"],
                    "cache_creation": row["cache_creation_input_tokens"],
                    "cache_read": row["cache_read_input_tokens"],
                },
                model,
                pricing_table,
                aliases=aliases,
            )
            counted = usage_key not in seen_usage
            if counted:
                seen_usage.add(usage_key)
                tokens = session_tokens.setdefault(
                    row["session_id"],
                    {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "reasoning": 0},
                )
                tokens["input"] += int(row["input_tokens"] or 0)
                tokens["output"] += int(row["output_tokens"] or 0)
                tokens["cache_creation"] += int(row["cache_creation_input_tokens"] or 0)
                tokens["cache_read"] += int(row["cache_read_input_tokens"] or 0)
                tokens["reasoning"] += int(row["reasoning_output_tokens"] or 0)
                session_costs[row["session_id"]] = (
                    session_costs.get(row["session_id"], 0.0) + breakdown.total_cost_usd
                )
            conn.execute(
                """
                UPDATE llm_calls
                SET
                  estimated_cost_usd = ?,
                  request_model = CASE
                    WHEN COALESCE(request_model, '') = '' THEN NULLIF(?, '')
                    ELSE request_model
                  END,
                  response_model = CASE
                    WHEN COALESCE(response_model, '') = '' THEN NULLIF(?, '')
                    ELSE response_model
                  END
                WHERE id = ?
                """,
                (
                    breakdown.total_cost_usd if counted else 0.0,
                    model,
                    model,
                    row["id"],
                ),
            )
        timestamp = datetime.now(tz=UTC).isoformat()
        session_scope = f"AND id IN ({placeholders})" if scoped_ids is not None else ""
        session_level_rows = conn.execute(
            f"""
            SELECT
              id,
              source_ref,
              input_tokens,
              output_tokens,
              cache_creation_tokens,
              cache_read_tokens,
              reasoning_tokens
            FROM sessions
            WHERE COALESCE(input_tokens, 0)
                + COALESCE(output_tokens, 0)
                + COALESCE(cache_creation_tokens, 0)
                + COALESCE(cache_read_tokens, 0)
                + COALESCE(reasoning_tokens, 0) > 0
              {session_scope}
            """,
            scoped_ids or [],
        ).fetchall()
        for session_row in session_level_rows:
            session_id = session_row["id"]
            exact_tokens = session_tokens.get(session_id, {})
            exact_token_total = sum(int(value or 0) for value in exact_tokens.values())
            if exact_token_total > 0:
                continue
            model = session_model_hints.get(session_id) or session_models.get(session_id, "")
            if not model:
                parent_session_id = cursor_native_parent_session_id(str(session_row["source_ref"] or ""))
                if parent_session_id and parent_session_id != session_id:
                    model = session_model_hints.get(parent_session_id) or session_models.get(parent_session_id, "")
            if not model:
                continue
            breakdown = calculate_cost(
                {
                    "input": session_row["input_tokens"],
                    "output": session_row["output_tokens"],
                    "cache_creation": session_row["cache_creation_tokens"],
                    "cache_read": session_row["cache_read_tokens"],
                },
                model,
                pricing_table,
                aliases=aliases,
            )
            if not breakdown.resolution.matched_model_key:
                continue
            session_costs[session_id] = max(session_costs.get(session_id, 0.0), breakdown.total_cost_usd)
        for session_id, total_cost in session_costs.items():
            tokens = session_tokens.get(session_id, {})
            token_total = (
                tokens.get("input", 0)
                + tokens.get("output", 0)
                + tokens.get("cache_creation", 0)
                + tokens.get("cache_read", 0)
                + tokens.get("reasoning", 0)
            )
            if token_total <= 0 and total_cost <= 0:
                continue
            conn.execute(
                """
                UPDATE sessions
                SET
                  input_tokens = CASE WHEN ? > 0 THEN ? ELSE input_tokens END,
                  output_tokens = CASE WHEN ? > 0 THEN ? ELSE output_tokens END,
                  cache_creation_tokens = CASE WHEN ? > 0 THEN ? ELSE cache_creation_tokens END,
                  cache_read_tokens = CASE WHEN ? > 0 THEN ? ELSE cache_read_tokens END,
                  reasoning_tokens = CASE WHEN ? > 0 THEN ? ELSE reasoning_tokens END,
                  estimated_cost_usd = ?,
                  updated_at = ?
                WHERE id = ?
                """,
                (
                    token_total,
                    tokens.get("input", 0),
                    token_total,
                    tokens.get("output", 0),
                    token_total,
                    tokens.get("cache_creation", 0),
                    token_total,
                    tokens.get("cache_read", 0),
                    token_total,
                    tokens.get("reasoning", 0),
                    total_cost,
                    timestamp,
                    session_id,
                ),
            )
        conn.commit()
    finally:
        conn.row_factory = previous_row_factory
