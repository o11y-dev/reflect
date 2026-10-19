from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import click

from reflect.cli.common import REFLECT_HOME, echo_json, require_snapshot_schema
from reflect.preparation import PreparationLock
from reflect.preparation_pipeline import prepare_sql_report_db

DEFAULT_DB_PATH = REFLECT_HOME / "state" / "reflect.db"


@click.group()
def db() -> None:
    """SQLite store management commands."""


def _ingest_into_db(
    *,
    db_path: Path,
    otlp_traces: Path | None = None,
    spans_file: Path | None = None,
) -> dict[str, int]:
    from reflect.store.ingest import ingest_local_spans_file, ingest_otlp_traces_file
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    if (otlp_traces is None) == (spans_file is None):
        raise click.ClickException("Pass exactly one of --otlp or --spans-file")

    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        migrate(conn)
        mark_snapshot(conn, ready=False)
        if otlp_traces is not None:
            result = ingest_otlp_traces_file(conn, file_path=otlp_traces)
        else:
            result = ingest_local_spans_file(conn, file_path=spans_file)
    # Reuse the same publication path as every normal refresh, including graph
    # and workflow state. A failure leaves the durable incomplete marker intact.
    prepare_sql_report_db(db_path, otlp_traces=None)
    return result


@click.command("ingest")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--otlp", "otlp_traces", type=click.Path(path_type=Path), default=None, help="Path to OTLP traces JSONL export file.")
@click.option("--spans-file", type=click.Path(path_type=Path), default=None, help="Path to local hook spans JSONL file.")
def ingest(db_path: Path, otlp_traces: Path | None, spans_file: Path | None) -> None:
    """Ingest telemetry records into raw_events."""
    source_path = otlp_traces or spans_file
    result = _ingest_into_db(db_path=db_path, otlp_traces=otlp_traces, spans_file=spans_file)
    click.echo(
        f"Ingested {source_path} -> {db_path} (inserted={result['inserted']}, skipped={result['skipped']})"
    )


@db.command("ingest-spans")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--spans-file", type=click.Path(path_type=Path), required=True, help="Path to local hook spans JSONL file.")
def db_ingest_spans(db_path: Path, spans_file: Path) -> None:
    """Ingest local hook spans JSONL into raw_events with source/hash dedupe."""
    result = _ingest_into_db(db_path=db_path, spans_file=spans_file)
    click.echo(
        f"Ingested {spans_file} -> {db_path} (inserted={result['inserted']}, skipped={result['skipped']})"
    )


@db.command("normalize")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--limit", type=int, default=None, help="Maximum pending raw_events to normalize.")
def db_normalize(db_path: Path, limit: int | None) -> None:
    """Normalize pending raw_events into canonical SQLite tables."""
    from reflect.store.migrate import migrate
    from reflect.store.normalize import backfill_mcp_calls, normalize_pending_raw_events
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        applied = migrate(conn)
        mark_snapshot(conn, ready=False)
        changed_session_ids: set[str] = set()
        result = normalize_pending_raw_events(
            conn,
            limit=limit,
            changed_session_ids=changed_session_ids,
        )
        backfill_mcp_calls(
            conn,
            session_ids=None if 14 in applied else changed_session_ids,
        )
    click.echo(
        "Normalized raw_events "
        f"(processed={result['processed']}, failed={result['failed']}, skipped={result['skipped']})"
    )

    click.echo("Run `reflect refresh` to publish a complete snapshot.")


@db.command("rebuild-graph")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
def db_rebuild_graph(db_path: Path) -> None:
    """Rebuild graph_nodes and graph_edges from canonical SQLite tables."""
    from reflect.store.graph_normalize import rebuild_graph
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        migrate(conn)
        mark_snapshot(conn, ready=False)
        result = rebuild_graph(conn)
    click.echo(f"Rebuilt graph (nodes={result['nodes']}, edges={result['edges']})")

    click.echo("Run `reflect refresh` to publish a complete snapshot.")


@db.command("rebuild-rollups")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
def db_rebuild_rollups(db_path: Path) -> None:
    """Rebuild aggregate rollup tables from canonical SQLite tables."""
    from reflect.store.migrate import migrate
    from reflect.store.rollups import rebuild_rollups
    from reflect.store.sqlite import connect_sqlite, mark_snapshot

    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        migrate(conn)
        mark_snapshot(conn, ready=False)
        result = rebuild_rollups(conn)
    click.echo(
        "Rebuilt rollups "
        f"(sessions={result['session_rollups']}, days={result['daily_rollups']}, tools={result['tool_rollups']})"
    )

    click.echo("Run `reflect refresh` to publish a complete snapshot.")


@db.command("migrate")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
def db_migrate(db_path: Path) -> None:
    """Apply pending SQLite migrations."""
    from reflect.store.migrate import migrate
    from reflect.store.sqlite import connect_sqlite

    with PreparationLock(db_path), closing(connect_sqlite(db_path)) as conn:
        applied = migrate(conn)

    if not applied:
        click.echo(f"No pending migrations for {db_path}")
        return
    click.echo(f"Applied migrations to {db_path}: {', '.join(str(v) for v in applied)}")


@db.command("doctor")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
def db_doctor(db_path: Path) -> None:
    """Inspect SQLite store migration, pragma, and foreign-key health."""
    from reflect.store.doctor import inspect_database
    from reflect.store.sqlite import connect_sqlite

    conn = connect_sqlite(db_path)
    try:
        status = inspect_database(conn)
    finally:
        conn.close()

    click.echo(f"SQLite DB: {db_path}")
    applied = ", ".join(str(version) for version in status["applied_migrations"]) or "none"
    expected = ", ".join(str(version) for version in status["expected_migrations"]) or "none"
    click.echo(f"Migrations: applied={applied}; expected={expected}")
    if status["pending_migrations"]:
        pending = ", ".join(str(version) for version in status["pending_migrations"])
        click.echo(f"Pending migrations: {pending}")
    if status["unknown_migrations"]:
        unknown = ", ".join(str(version) for version in status["unknown_migrations"])
        click.echo(f"Unknown migrations: {unknown}")

    foreign_key_issues = status["foreign_key_issues"]
    if foreign_key_issues:
        click.echo(f"Foreign keys: {len(foreign_key_issues)} issue(s)")
    else:
        click.echo("Foreign keys: ok")

    pragmas = status["pragmas"]
    click.echo(
        "Pragmas: "
        f"foreign_keys={pragmas['foreign_keys']}, "
        f"journal_mode={pragmas['journal_mode']}, "
        f"synchronous={pragmas['synchronous']}, "
        f"wal_autocheckpoint={pragmas['wal_autocheckpoint']}, "
        f"busy_timeout={pragmas['busy_timeout']}"
    )
    if status["ok"]:
        click.echo("SQLite store health: ok")
        return

    click.echo("SQLite store health: needs attention")
    raise click.ClickException("SQLite store health checks failed")


@db.command("prune-sessions")
@click.option(
    "--older-than-days",
    type=click.IntRange(min=1),
    default=60,
    show_default=True,
    help="Prune only invalid-start sessions with no trusted activity for this many days.",
)
@click.option(
    "--all-inactive-sessions",
    is_flag=True,
    help="Also prune inactive sessions with valid timestamps older than the cutoff.",
)
@click.option("--apply", "apply_changes", is_flag=True, help="Apply the previewed deletion.")
@click.option(
    "--backup/--no-backup",
    default=True,
    help="Create a timestamped database backup before applying.",
)
@click.option(
    "--vacuum",
    is_flag=True,
    help="Reclaim disk space after applying. Never runs during a dry run.",
)
@click.option("--json", "as_json", is_flag=True, help="Print the result as JSON.")
@click.option(
    "--db-path",
    type=click.Path(path_type=Path),
    default=DEFAULT_DB_PATH,
)
def db_prune_sessions(
    older_than_days: int,
    all_inactive_sessions: bool,
    apply_changes: bool,
    backup: bool,
    vacuum: bool,
    as_json: bool,
    db_path: Path,
) -> None:
    """Preview or prune inactive sessions selected by the retention policy."""
    from dataclasses import asdict

    from reflect.cli.progress import TerminalPreparationProgress
    from reflect.preparation import PreparationStage, report_preparation_progress
    from reflect.store.migrate import migrate
    from reflect.store.retention import SessionPruner, SessionRetentionPolicy
    from reflect.store.sqlite import (
        SQLiteBackupProgress,
        advance_snapshot_revision,
        backup_sqlite,
        connect_sqlite,
        connect_sqlite_read_only,
    )

    if vacuum and not apply_changes:
        raise click.UsageError("--vacuum requires --apply")
    if not db_path.exists():
        raise click.ClickException(f"SQLite store not found: {db_path}")

    backup_path: Path | None = None
    if not apply_changes:
        require_snapshot_schema(
            db_path,
            refresh_hint=(
                f"Run `reflect db migrate --db-path {db_path}` before previewing "
                "session retention."
            ),
        )
        conn = connect_sqlite_read_only(db_path)
        try:
            result = SessionPruner(
                conn,
                SessionRetentionPolicy(
                    older_than_days=older_than_days,
                    include_valid_starts=all_inactive_sessions,
                ),
            ).run()
        finally:
            conn.close()
    else:
        def apply_pruning(progress):
            report_preparation_progress(
                progress,
                PreparationStage.OPENING_STORE,
                "Opening the local telemetry store...",
            )
            with PreparationLock(db_path):
                conn = connect_sqlite(db_path)
                applied_backup_path: Path | None = None
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if backup:
                        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
                        applied_backup_path = db_path.with_name(
                            f"{db_path.name}.backup-{stamp}"
                        )
                        report_preparation_progress(
                            progress,
                            PreparationStage.BACKING_UP_STORE,
                            "Backing up the local telemetry store...",
                        )
                        last_backup_percent = -5

                        def update_backup(snapshot: SQLiteBackupProgress) -> None:
                            nonlocal last_backup_percent
                            percent = snapshot.percent_complete
                            if percent < 100 and percent < last_backup_percent + 5:
                                return
                            last_backup_percent = percent
                            report_preparation_progress(
                                progress,
                                PreparationStage.BACKING_UP_STORE,
                                f"Backing up the local telemetry store... {percent}% "
                                f"({snapshot.completed_pages:,}/{snapshot.total_pages:,} pages)",
                            )

                        backup_sqlite(
                            db_path,
                            applied_backup_path,
                            progress=update_backup,
                        )
                    report_preparation_progress(
                        progress,
                        PreparationStage.MIGRATING_SCHEMA,
                        "Checking database migrations...",
                    )
                    migrate(conn, commit=False)
                    report_preparation_progress(
                        progress,
                        PreparationStage.PRUNING_SESSIONS,
                        "Pruning eligible sessions and updating affected derived data...",
                    )
                    applied_result = SessionPruner(
                        conn,
                        SessionRetentionPolicy(
                            older_than_days=older_than_days,
                            include_valid_starts=all_inactive_sessions,
                        ),
                    ).run(apply=True)
                    advance_snapshot_revision(conn)
                    conn.commit()
                    if vacuum and applied_result.pruned_session_ids:
                        report_preparation_progress(
                            progress,
                            PreparationStage.VACUUMING_STORE,
                            "Vacuuming the local telemetry store...",
                        )
                        conn.execute("VACUUM")
                    report_preparation_progress(
                        progress,
                        PreparationStage.COMPLETE,
                        "Session pruning complete.",
                    )
                    return applied_result, applied_backup_path
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                finally:
                    conn.close()

        result, backup_path = TerminalPreparationProgress().run(
            apply_pruning,
            initial_message="Preparing session pruning...",
        )

    payload = {
        "dry_run": result.dry_run,
        "older_than_days": older_than_days,
        "all_inactive_sessions": all_inactive_sessions,
        "candidate_count": len(result.candidates),
        "candidates": [asdict(candidate) for candidate in result.candidates],
        "pruned_session_ids": list(result.pruned_session_ids),
        "backup_path": str(backup_path) if backup_path else None,
        "vacuumed": bool(vacuum and result.pruned_session_ids),
        "foreign_key_violations": [list(row) for row in result.foreign_key_violations],
        "graph": result.graph,
        "rollups": result.rollups,
    }
    if as_json:
        echo_json(payload)
        return
    action = "Pruned" if apply_changes else "Would prune"
    session_scope = "inactive" if all_inactive_sessions else "invalid-start"
    click.echo(
        f"{action} {len(result.candidates)} {session_scope} session(s) "
        f"with no trusted activity for {older_than_days} days."
    )
    for candidate in result.candidates:
        click.echo(
            f"  {candidate.session_id} · {candidate.agent or 'unknown'} · "
            f"{candidate.last_observed_at or 'no trusted activity'} · "
            f"{sum(candidate.dependent_rows.values())} dependent row(s)"
        )
    if backup_path:
        click.echo(f"Backup: {backup_path}")
    if not apply_changes and result.candidates:
        click.echo("Dry run only. Re-run with --apply to prune these exact policy matches.")
