from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import Future, wait
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
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
    from reflect.store.git_activity import refresh_git_activity
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
    from reflect.store.pending_refresh import PendingRefreshState
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
        pending_state = PendingRefreshState(conn)
        from reflect.task_inbox import TaskEventInbox
        TaskEventInbox(db_path).drain(conn=conn)
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

        changed_session_ids = pending_state.capture().pending_for("canonical")
        normalization_failed = 0
        if conn.execute(
            "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
        ).fetchone() or pending_state.capture().pending_for("canonical"):
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
        usage_inputs = pending_state.capture()
        changed_session_ids.update(usage_inputs.pending_for("cost") | usage_inputs.pending_for("rollup"))
        conn.commit()
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
        if cost_inputs is not None:
            cost_state.mark_current(cost_inputs)
        # Normalization projects new calls. Context repairs after normalization
        # can change their repository; finish that remaining scope before ack.
        refresh_git_activity(conn, session_ids=None if 39 in applied else usage_inputs.pending_for("canonical"))
        for phase in ("canonical", "cost", "rollup"):
            pending_state.acknowledge(usage_inputs, phase)
        conn.commit()
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
    native_session_ids: tuple[str, ...] = (),
    spans_dir: Path | None = None,
    progress: PreparationProgressReporter | None = None,
    defer_otlp_replay: bool = False,
    keep_processed_raw: bool = False,
    analysis_days: int | None = None,
    analysis_session_ids: set[str] | None = None,
    usage_ready: Callable[[dict[str, object]], Future[float] | None] | None = None,
) -> dict[str, object]:
    from reflect.preparation import PreparationLock, PreparationStage, report_preparation_progress
    from reflect.store.analysis_window import (
        AnalysisWindow,
        analysis_inputs_fingerprint,
        historical_cohort_session_ids,
    )
    from reflect.store.cost_refresh import CostRefreshState
    from reflect.store.cursor_usage import (
        apply_cursor_transcript_usage_estimates,
        repair_misattributed_cursor_transcript_usage,
    )
    from reflect.store.git_activity import refresh_git_activity
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
    from reflect.store.pending_refresh import PendingRefreshState
    from reflect.store.refresh_plan import RefreshMode, plan_derived_refresh, session_batches
    from reflect.store.rollups import rebuild_rollups, refresh_rollups, rollup_rebuild_pending
    from reflect.store.sqlite import connect_sqlite, mark_snapshot, snapshot_incomplete
    from reflect.store.workspaces import backfill_session_context
    from reflect.task_runs import TaskReportRefreshState
    from reflect.utils import _json_dumps

    started = perf_counter()
    timings: dict[str, float] = {}
    report_preparation_progress(
        progress,
        PreparationStage.WAITING_FOR_STORE,
        "Waiting for any active refresh of this store...",
    )
    with (
        PreparationLock(db_path),
        closing(connect_sqlite(db_path)) as conn,
        ExitStack() as publication_cleanup,
    ):
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
        from reflect.store.evidence_revision import install_evidence_revision_tracking
        install_evidence_revision_tracking(conn)
        conn.commit()  # Inbox drain owns its receipt/projection transaction.
        pending_state = PendingRefreshState(conn)
        checkpoint_initialized = pending_state.initialized()
        window = AnalysisWindow.capture(analysis_days)
        from reflect.task_inbox import TaskEventInbox
        TaskEventInbox(db_path).drain(conn=conn)
        report_state = TaskReportRefreshState(conn)
        initial_report_inputs = report_state.inspect()
        recovering = snapshot_incomplete(conn)
        mark_snapshot(conn, ready=False)
        phase_started = perf_counter()
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
                if native_session_ids and agent != 'opencode' and not any(
                    _native_session_path_matches_id(session_file, candidate)
                    for candidate in native_session_ids
                ):
                    continue
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
                    session_ids=native_session_ids,
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
            or pending_state.capture().pending_for("canonical")
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE normalized_status = 'pending' LIMIT 1"
            ).fetchone()
            or conn.execute(
                "SELECT 1 FROM raw_events WHERE origin_kind IS NULL LIMIT 1"
            ).fetchone()
        )
        changed_session_ids = pending_state.capture().pending_for("canonical")
        timings["ingest_seconds"] = perf_counter() - phase_started
        phase_started = perf_counter()
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
        timings["normalize_seconds"] = perf_counter() - phase_started
        phase_started = perf_counter()
        report_preparation_progress(
            progress,
            PreparationStage.UPDATING_CANONICAL_STATE,
            "Checking captured tool activity...",
        )
        fingerprint_result = backfill_tool_call_hashes(conn)
        report_preparation_progress(
            progress, PreparationStage.UPDATING_CANONICAL_STATE,
            "Linking captured MCP activity...",
        )
        mcp_backfill_result = backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
            changed_session_ids=changed_session_ids,
        )
        report_preparation_progress(
            progress, PreparationStage.UPDATING_CANONICAL_STATE,
            "Checking usage attribution...",
        )
        operation_started = perf_counter()
        cursor_provenance_result = repair_misattributed_cursor_transcript_usage(conn)
        timings["canonical_cursor_repair_seconds"] = perf_counter() - operation_started
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_provenance_result["session_ids"]
            if session_id
        )
        report_preparation_progress(
            progress, PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating session context...",
        )
        operation_started = perf_counter()
        context_result = backfill_session_context(
            conn,
            timestamp=datetime.now(UTC).isoformat(),
            changed_session_ids=changed_session_ids,
        )
        timings["canonical_context_seconds"] = perf_counter() - operation_started
        report_preparation_progress(
            progress, PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating transcript usage estimates...",
        )
        operation_started = perf_counter()
        cursor_usage_result = apply_cursor_transcript_usage_estimates(
            conn,
            cursor_native_files,
        )
        timings["canonical_transcript_usage_seconds"] = perf_counter() - operation_started
        changed_session_ids.update(
            str(session_id)
            for session_id in cursor_usage_result.get("session_ids", [])
            if session_id
        )
        # Keep old report scope before normalization/reconciliation can move it.
        # Persist this captured input only after successful derivation. Reports
        # projected during later commits will then remain dirty next refresh.
        report_inputs = report_state.inspect()
        reconciled_legacy_data = rollup_rebuild_pending(conn)
        all_session_ids = {str(row[0]) for row in conn.execute("SELECT id FROM sessions")}
        # Reports may arrive before their telemetry. There is no derived work
        # for those IDs yet; later ingestion will add the canonical session.
        report_changes = (initial_report_inputs.session_ids | report_inputs.session_ids) & all_session_ids
        changed_session_ids.update(report_changes)
        pending_state.mark(report_changes)
        hot_session_ids = window.session_ids(conn)
        if analysis_session_ids is not None:
            hot_session_ids = analysis_session_ids & all_session_ids
        captured_changes = pending_state.capture().pending_for()
        if not checkpoint_initialized:
            # An upgrade has no durable phase history. Repair accounting without
            # forcing historical analytics onto the interactive publication path.
            pending_state.initialize()
            if recovering:
                pending_state.mark(all_session_ids, phases=("cost", "rollup"))
                pending_state.mark(hot_session_ids, phases=("graph", "improvements"))
        if 39 in applied:
            pending_state.mark(all_session_ids, phases=("cost", "rollup"))
        if 44 in applied:
            obsolete_graph = {str(row[0]) for row in conn.execute(
                "SELECT session_id FROM pending_refresh WHERE phase='graph' AND revision='compact-ontology-v2'"
            )}
            pending_state.defer(obsolete_graph - hot_session_ids, phases=("graph",))
        if 45 in applied:
            existing_evidence_work = pending_state.capture().pending_for("improvements", include_deferred=False)
            pending_state.mark(all_session_ids, phases=("improvements",))
            pending_state.defer(all_session_ids - hot_session_ids - existing_evidence_work, phases=("improvements",))
        if not checkpoint_initialized:
            previous_graph = {str(row[0]) for row in conn.execute(
                "SELECT session_id FROM graph_nodes WHERE kind='Session' AND session_id IS NOT NULL"
            )}
            previous_units = {str(row[0]) for row in conn.execute("SELECT DISTINCT session_id FROM execution_units")}
            previously_analysed = previous_graph | previous_units
            pending_state.mark(all_session_ids - previous_graph, phases=("graph",))
            pending_state.mark(all_session_ids - previous_units, phases=("improvements",))
            deferred_history = all_session_ids - hot_session_ids - previously_analysed
            if recovering:
                # Before durable checkpoints existed an interrupted refresh
                # cannot prove historical analysis current. Keep it explicitly
                # pending rather than silently trusting or rebuilding it all.
                deferred_history |= all_session_ids - hot_session_ids - captured_changes
            pending_state.mark(deferred_history, phases=("graph", "improvements"))
            pending_state.defer(deferred_history, phases=("graph", "improvements"))
        conn.commit()
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
        pending_inputs = pending_state.capture()
        cost_targets = pending_inputs.pending_for("cost")
        graph_targets = pending_inputs.pending_for("graph", include_deferred=False)
        graph_targets |= pending_inputs.pending_for("graph") & hot_session_ids
        analysis_scope = hot_session_ids | graph_targets | pending_inputs.pending_for("improvements", include_deferred=False)
        historical_cohort = historical_cohort_session_ids(
            conn, corrected_session_ids=analysis_scope, hot_session_ids=hot_session_ids,
        )
        analysis_scope.update(historical_cohort)
        cost_state = CostRefreshState(conn)
        report_preparation_progress(
            progress, PreparationStage.UPDATING_CANONICAL_STATE,
            "Updating usage costs...",
        )
        operation_started = perf_counter()
        cost_inputs = (
            cost_state.inspect(session_ids=cost_targets)
            if all_session_ids
            else None
        )
        if cost_inputs is not None and cost_inputs.requires_full_refresh:
            priced_evidence = hot_session_ids | {
                str(row[0]) for row in conn.execute("SELECT DISTINCT session_id FROM execution_units")
            }
            pending_state.mark(priced_evidence, phases=("improvements",))
            conn.commit()
            pending_inputs = pending_state.capture()
            analysis_scope.update(priced_evidence)
        refresh_plan = plan_derived_refresh(
            changed_session_ids=cost_targets | pending_inputs.pending_for("rollup"),
            all_session_ids=all_session_ids,
            graph_session_ids=graph_session_ids,
            rollup_session_ids=rollup_session_ids,
            graph_exists=bool(graph_session_ids),
            graph_scope_session_ids=hot_session_ids,
            graph_changed_session_ids=graph_targets,
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
        if refresh_plan.cost_mode is not RefreshMode.FULL:
            refresh_plan = replace(
                refresh_plan,
                cost_mode=RefreshMode.INCREMENTAL if cost_targets else RefreshMode.SKIP,
                cost_session_ids=tuple(sorted(cost_targets)),
            )

        if refresh_plan.cost_mode is RefreshMode.FULL:
            ensure_sql_costs(conn, refresh_inputs=cost_inputs)
        elif refresh_plan.cost_mode is RefreshMode.INCREMENTAL:
            for batch in session_batches(refresh_plan.cost_session_ids):
                ensure_sql_costs(conn, session_ids=set(batch), refresh_inputs=cost_inputs)
                pending_state.acknowledge(pending_inputs, "cost", session_ids=set(batch))
                conn.commit()
        pending_state.acknowledge(pending_inputs, "cost")
        operation_git_started = perf_counter()
        refresh_git_activity(conn, session_ids=None if 39 in applied else pending_inputs.pending_for("canonical"))
        timings["git_activity_seconds"] = perf_counter() - operation_git_started
        pending_state.acknowledge(pending_inputs, "canonical")
        conn.commit()

        timings["canonical_pricing_seconds"] = perf_counter() - operation_started
        timings["canonical_seconds"] = perf_counter() - phase_started
        phase_started = perf_counter()
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
            for batch in session_batches(rollup_targets):
                rollup_result = refresh_rollups(conn, set(batch))
                pending_state.acknowledge(pending_inputs, "rollup", session_ids=set(batch))
                conn.commit()
        else:
            rollup_result = {
                "session_rollups": len(rollup_session_ids),
                "daily_rollups": int(conn.execute("SELECT COUNT(*) FROM daily_rollups").fetchone()[0]),
                "tool_rollups": int(conn.execute("SELECT COUNT(*) FROM tool_rollups").fetchone()[0]),
                "skipped": 1,
                "reason": refresh_plan.rollup_reason,
            }
        if cost_inputs is not None:
            cost_state.mark_current(cost_inputs)
        timings["rollup_seconds"] = perf_counter() - phase_started
        pending_state.acknowledge(pending_inputs, "rollup")
        usage_completed_at = datetime.now(UTC).isoformat()
        window_details = window.as_dict(hot_session_ids, pending_history=len(
            pending_inputs.pending_for("graph") - analysis_scope
        ), historical_cohort_sessions=len(historical_cohort - hot_session_ids))
        for key, value in (
            ("last_successful_usage_refresh", usage_completed_at),
            ("analysis_window_v1", _json_dumps(window_details)),
        ):
            conn.execute(
                "INSERT INTO store_metadata(key,value,updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, usage_completed_at),
            )
        conn.commit()
        mark_snapshot(conn, ready=True, profile="usage")
        operation_started = perf_counter()
        publication = (
            usage_ready({"completed_at": usage_completed_at, "analysis_window": dict(window_details)})
            if usage_ready is not None else None
        )
        if publication is not None:
            # Keep the store lock until publication settles, including on an
            # analysis failure. Waiting here must not replace that failure.
            publication_cleanup.callback(wait, [publication])
        timings["usage_publication_seconds"] = perf_counter() - operation_started
        phase_started = perf_counter()
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
            graph_result = {"nodes": 0, "edges": 0, "refreshed_sessions": 0}
            for batch in session_batches(graph_targets):
                scopes = pending_inputs.step_scope("graph")
                precise = all(scopes.get(sid) is not None for sid in batch)
                batch_result = refresh_graph(
                    conn, set(batch),
                    changed_step_ids=set().union(*(scopes[sid] for sid in batch)) if precise else None,
                )
                for key in graph_result:
                    graph_result[key] += int(batch_result.get(key, 0))
                pending_state.acknowledge(pending_inputs, "graph", session_ids=set(batch))
                conn.commit()
        else:
            graph_result = {
                "nodes": 0,
                "edges": 0,
                "skipped": 1,
                "reason": refresh_plan.graph_reason,
            }

        timings["graph_seconds"] = perf_counter() - phase_started
        pending_state.acknowledge(pending_inputs, "graph", session_ids=set(refresh_plan.graph_session_ids))
        conn.commit()
        phase_started = perf_counter()
        from reflect.improvements.service import ImprovementService
        from reflect.memory.sqlite_provider import LocalSQLiteMemoryProvider
        LocalSQLiteMemoryProvider(conn, maintain_search_index=False).validate_sources()

        # Existing stores may have canonical evidence but no execution units.
        # Repair those sessions without rederiving unrelated historical work.
        missing_evidence_sessions = {
            str(row[0]) for row in conn.execute(
                """SELECT s.id FROM sessions s
                   WHERE EXISTS (SELECT 1 FROM steps st WHERE st.session_id = s.id)
                     AND NOT EXISTS (
                       SELECT 1 FROM execution_units eu
                       WHERE eu.session_id = s.id
                     )"""
            )
        }
        # Complete historical cohorts are detector context, not new evidence
        # work. Retain every durable correction, but only repair deferred or
        # missing evidence automatically inside the selected activity window.
        evidence_session_ids = pending_inputs.pending_for("improvements", include_deferred=False) | (
            (pending_inputs.pending_for("improvements") | missing_evidence_sessions) & hot_session_ids
        )
        from reflect.execution_units import ExecutionUnitService

        requested_evidence_ids = evidence_session_ids
        consumer_unit_ids: dict[str, set[str]] = {}
        affected = ExecutionUnitService(conn).affected_session_ids(
            evidence_session_ids, step_scope=pending_inputs.step_scope("improvements"),
            consumer_unit_ids=consumer_unit_ids,
        )
        evidence_session_ids = all_session_ids if affected is None else affected
        reassess_unit_ids = {session: units for session, units in consumer_unit_ids.items()
                             if session not in pending_inputs.pending_for("improvements")}
        # Overlap dependencies can discover historical sessions after the first
        # window/cohort selection. Give detectors their complete repo cohort,
        # while stable peers are context only, not new dependency seeds.
        discovered_cohort = historical_cohort_session_ids(
            conn, corrected_session_ids=evidence_session_ids - analysis_scope,
            hot_session_ids=hot_session_ids,
        )
        if discovered_cohort:
            analysis_scope.update(discovered_cohort)
            historical_cohort.update(discovered_cohort)
            window_details["historical_cohort_sessions"] = len(historical_cohort - hot_session_ids)
            conn.execute(
                "UPDATE store_metadata SET value=?, updated_at=? WHERE key='analysis_window_v1'",
                (_json_dumps(window_details), datetime.now(UTC).isoformat()),
            )
        # Persist dependency expansion before execution ownership can change.
        pending_state.mark(evidence_session_ids - pending_inputs.pending_for("improvements", include_deferred=False),
                           phases=("improvements",))
        conn.commit()
        evidence_inputs = pending_state.capture()
        service = ImprovementService(conn)
        fingerprint = analysis_inputs_fingerprint(
            hot_session_ids, days=analysis_days,
            contracts=service._contract_inputs_fingerprint(),
        )
        previous = conn.execute(
            "SELECT value FROM store_metadata WHERE key='analysis_inputs_v1'"
        ).fetchone()
        analysis_changed = bool(
            evidence_session_ids or not previous or previous[0] != fingerprint
            or (cost_inputs is not None and cost_inputs.requires_full_refresh)
            or initial_report_inputs.requires_full_refresh or report_inputs.requires_full_refresh
        )
        report_preparation_progress(
            progress,
            PreparationStage.REFRESHING_IMPROVEMENTS,
            "Refreshing evidence-backed improvements...",
        )
        timings["improvement_dependencies_seconds"] = perf_counter() - phase_started
        if analysis_days is None and analysis_session_ids is None:
            improvement_result = service.refresh(
                session_ids=evidence_session_ids, measure=False, dependencies_expanded=True,
                step_scope=evidence_inputs.step_scope("improvements"),
                reassess_unit_ids=reassess_unit_ids,
            )
        else:
            improvement_result = service.refresh(
                session_ids=evidence_session_ids,
                analysis_session_ids=(analysis_scope | evidence_session_ids) if analysis_changed else set(),
                measure=False, dependencies_expanded=True,
                step_scope=evidence_inputs.step_scope("improvements"),
                reassess_unit_ids=reassess_unit_ids,
            )
        pending_state.acknowledge(evidence_inputs, "improvements",
                                  session_ids=evidence_session_ids | requested_evidence_ids)
        conn.execute(
            "INSERT INTO store_metadata(key,value,updated_at) VALUES ('analysis_inputs_v1', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (fingerprint, datetime.now(UTC).isoformat()),
        )
        conn.commit()
        measurement_result = service.measurements.measure_active()
        if measurement_result["created"]:
            service.skills.sync_measurements()
        improvement_result.update(
            measurements_created=measurement_result["created"],
            regressions=measurement_result["regressed"],
        )
        timings.update({f"improvement_{name}_seconds": value for name, value in service.timings.items()})
        timings["improvements_seconds"] = perf_counter() - phase_started
        phase_started = perf_counter()
        if publication is not None:
            # Final publication advances prepared_usage as well as the full
            # snapshot. Finish composed usage reads before changing its revision.
            timings["usage_publication_seconds"] = publication.result()
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
        report_state.mark_current(report_inputs)
        timings["pipeline_before_publication_seconds"] = perf_counter() - started
        conn.execute(
            "INSERT INTO store_metadata(key,value,updated_at) VALUES ('last_refresh_summary_v1', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_json_dumps({"completed_at": refresh_completed_at, "timings": timings,
                          "changed_sessions": len(changed_session_ids),
                          "analysis_window": window_details, "refresh_plan": refresh_plan.as_dict()}),
             refresh_completed_at),
        )
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
        # Completion callbacks can reuse this run's usage publication only
        # while these committed markers still identify the same generation.
        publication_revision = [tuple(row) for row in conn.execute(
            "SELECT key,value,updated_at FROM store_metadata "
            "WHERE key IN ('prepared_snapshot','prepared_usage') ORDER BY key"
        )]
        timings["publish_seconds"] = perf_counter() - phase_started
    timings["total_seconds"] = perf_counter() - started
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
        "changed_sessions": len(changed_session_ids),
        "analysis_window": window_details,
        "publication_revision": publication_revision,
        "timings": timings,
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
    from reflect.pricing import calculate_cost, load_pricing_table, pricing_inputs_fingerprint
    from reflect.usage_facts import usage_facts
    from reflect.utils import _json_dumps, _json_loads

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
        llm_scope = f"WHERE lc.session_id IN ({placeholders})" if scoped_ids is not None else ""
        rows = conn.execute(
            f"""
            SELECT
              lc.id,
              lc.session_id,
              lc.provider,
              lc.request_model,
              lc.response_model,
              COALESCE(NULLIF(lc.response_model, ''), NULLIF(lc.request_model, '')) AS model,
              lc.input_tokens,
              lc.output_tokens,
              lc.cache_creation_input_tokens,
              lc.cache_read_input_tokens,
              lc.reasoning_output_tokens,
              lc.estimated_cost_usd,
              lc.raw_attrs_json
            FROM llm_calls lc JOIN steps st ON st.id=lc.step_id
            {llm_scope}
            ORDER BY st.started_at, lc.id
            """,
            scoped_ids or [],
        ).fetchall()
        rejected_model_sessions: set[str] = set()
        seen_usage: set[tuple] = set()
        pricing_fingerprint = pricing_inputs_fingerprint(pricing_table, aliases)
        session_costs: dict[str, float] = {}
        session_tokens: dict[str, dict[str, int]] = {}
        session_model_hints: dict[str, str] = {}
        for row in rows:
            attrs = _json_loads(row["raw_attrs_json"] or "{}")
            facts = usage_facts(dict(row), attrs)
            model = row["model"] or ""
            if attrs.get("reflect.model.provenance") in {"inferred", "config_default"}:
                model = ""
                rejected_model_sessions.add(row["session_id"])
            if model:
                session_model_hints.setdefault(row["session_id"], model)
            breakdown = calculate_cost(
                {
                    "input": facts.tokens["input_tokens"],
                    "output": facts.tokens["output_tokens"],
                    "cache_creation": facts.tokens["cache_creation_input_tokens"],
                    "cache_read": facts.tokens["cache_read_input_tokens"],
                },
                model,
                pricing_table,
                aliases=aliases,
                # The current price contract has one cache-write rate. Use it
                # only when the recorded five-minute bucket covers all writes;
                # an absent or mixed TTL cannot be priced as five-minute.
                unknown_rate_buckets=frozenset({"cache_creation"})
                if int(row["cache_creation_input_tokens"] or 0) > 0 and (
                    int(attrs.get("gen_ai.usage.cache_creation.1h_input_tokens") or 0) > 0
                    or attrs.get("gen_ai.usage.cache_creation.5m_input_tokens")
                    != int(row["cache_creation_input_tokens"] or 0)
                )
                else frozenset(),
            )
            counted = facts.present and facts.identity not in seen_usage
            if counted:
                seen_usage.add(facts.identity)
                tokens = session_tokens.setdefault(
                    row["session_id"],
                    {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0, "reasoning": 0},
                )
                tokens["input"] += facts.tokens["input_tokens"]
                tokens["output"] += facts.tokens["output_tokens"]
                tokens["cache_creation"] += facts.tokens["cache_creation_input_tokens"]
                tokens["cache_read"] += facts.tokens["cache_read_input_tokens"]
                tokens["reasoning"] += facts.tokens["reasoning_output_tokens"]
                session_costs[row["session_id"]] = (
                    session_costs.get(row["session_id"], 0.0) + breakdown.total_cost_usd
                )
            attrs["reflect.cost.source"] = breakdown.resolution.source
            attrs["reflect.cost.price_source"] = pricing_table.source
            attrs["reflect.cost.fingerprint"] = pricing_fingerprint
            attrs["reflect.cost.breakdown"] = {
                key: getattr(breakdown, key) if counted else 0.0 for key in (
                    "input_cost_usd", "output_cost_usd", "cache_creation_cost_usd",
                    "cache_read_cost_usd", "total_cost_usd",
                )
            }
            attrs["reflect.cost.breakdown"]["missing_price_buckets"] = list(breakdown.missing_price_buckets)
            values = (
                breakdown.total_cost_usd if counted else 0.0,
                _json_dumps(attrs),
                row["request_model"] or model or None,
                row["response_model"] or model or None,
            )
            if values == (
                row["estimated_cost_usd"], row["raw_attrs_json"],
                row["request_model"], row["response_model"],
            ):
                continue
            conn.execute(
                """
                UPDATE llm_calls
                SET
                  estimated_cost_usd = ?,
                  raw_attrs_json = ?,
                  request_model = ?,
                  response_model = ?
                WHERE id = ?
                """,
                (*values, row["id"]),
            )
        timestamp = datetime.now(tz=UTC).isoformat()
        session_scope = f"WHERE id IN ({placeholders})" if scoped_ids is not None else ""
        session_rows = conn.execute(
            f"""
            SELECT
              id,
              source_ref,
              input_tokens,
              output_tokens,
              cache_creation_tokens,
              cache_read_tokens,
              reasoning_tokens,
              estimated_cost_usd
            FROM sessions
            {session_scope}
            """,
            scoped_ids or [],
        ).fetchall()
        sessions = {row["id"]: row for row in session_rows}
        token_columns = (
            "input_tokens", "output_tokens", "cache_creation_tokens",
            "cache_read_tokens", "reasoning_tokens",
        )
        session_level_rows = [
            row for row in session_rows
            if sum(row[column] or 0 for column in token_columns) > 0
        ]
        # Request-level usage is authoritative. Only session-level tokens
        # without a usable LLM model need the step-JSON fallback.
        fallback_ids = [
            row["id"] for row in session_level_rows
            if row["id"] not in session_tokens and row["id"] not in session_model_hints
        ]
        session_models: dict[str, str] = {}
        if fallback_ids:
            model_rows = conn.execute(
                f"""
                SELECT
                  session_id,
                  CASE WHEN json_extract(raw_attrs_json, '$."reflect.model.provenance"')
                    IN ('inferred', 'config_default') THEN NULL ELSE COALESCE(
                    NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                    NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
                  ) END AS model,
                  COUNT(*) AS count
                FROM steps
                WHERE session_id IN ({', '.join('?' for _ in fallback_ids)})
                  AND COALESCE(
                    NULLIF(json_extract(raw_attrs_json, '$."gen_ai.response.model"'), ''),
                    NULLIF(json_extract(raw_attrs_json, '$."gen_ai.request.model"'), '')
                  ) IS NOT NULL
                GROUP BY session_id, model
                ORDER BY session_id ASC, count DESC
                """,
                fallback_ids,
            ).fetchall()
            for model_row in model_rows:
                if model_row["model"]:
                    session_models.setdefault(model_row["session_id"], model_row["model"])
                else:
                    rejected_model_sessions.add(model_row["session_id"])
        for session_row in session_level_rows:
            session_id = session_row["id"]
            if session_id in session_tokens:
                continue
            model = session_model_hints.get(session_id) or session_models.get(session_id, "")
            if not model:
                if session_id in rejected_model_sessions or cursor_native_parent_session_id(str(session_row["source_ref"] or "")):
                    rejected_model_sessions.add(session_id)
                    session_costs[session_id] = 0.0
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
                unknown_rate_buckets=frozenset({"cache_creation"}),
            )
            if not breakdown.resolution.matched_model_key:
                continue
            session_costs[session_id] = max(session_costs.get(session_id, 0.0), breakdown.total_cost_usd)
        for session_id, total_cost in session_costs.items():
            tokens = session_tokens.get(session_id, {})
            has_usage = session_id in session_tokens
            if not has_usage and total_cost <= 0 and session_id not in rejected_model_sessions:
                continue
            current = sessions[session_id]
            values = (
                *(tokens.get(key, 0) if has_usage else current[column] for key, column in zip(
                    ("input", "output", "cache_creation", "cache_read", "reasoning"), token_columns,
                    strict=True,
                )),
                total_cost,
            )
            if values == tuple(current[column] for column in (*token_columns, "estimated_cost_usd")):
                continue
            conn.execute(
                """
                UPDATE sessions
                SET
                  input_tokens = ?,
                  output_tokens = ?,
                  cache_creation_tokens = ?,
                  cache_read_tokens = ?,
                  reasoning_tokens = ?,
                  estimated_cost_usd = ?,
                  updated_at = ?
                WHERE id = ?
                """,
                (*values, timestamp, session_id),
            )
        conn.commit()
    finally:
        conn.row_factory = previous_row_factory
