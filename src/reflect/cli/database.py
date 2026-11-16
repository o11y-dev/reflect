from __future__ import annotations

from pathlib import Path

import click

from reflect.cli.backend import request, submit
from reflect.cli.common import REFLECT_HOME, echo_json

DEFAULT_DB_PATH = REFLECT_HOME / "state" / "reflect.db"


@click.group()
def db() -> None:
    """SQLite store management commands."""




@click.command("ingest")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--otlp", "otlp_traces", type=click.Path(path_type=Path), default=None, help="Path to OTLP traces JSONL export file.")
@click.option("--spans-file", type=click.Path(path_type=Path), default=None, help="Path to local hook spans JSONL file.")
@click.option("--wait", is_flag=True)
def ingest(db_path: Path, otlp_traces: Path | None, spans_file: Path | None, wait: bool = False) -> None:
    'Ingest telemetry records into raw_events.'
    if bool(otlp_traces) == bool(spans_file):
        raise click.UsageError('Pass exactly one of --otlp or --spans-file')
    submit(db_path, '/api/maintenance/database/ingest', {'otlp_traces': str(otlp_traces.resolve()) if otlp_traces else None, 'spans_file': str(spans_file.resolve()) if spans_file else None}, wait=wait)


@db.command("ingest-spans")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--spans-file", type=click.Path(path_type=Path), required=True, help="Path to local hook spans JSONL file.")
@click.option("--wait", is_flag=True)
def db_ingest_spans(db_path: Path, spans_file: Path, wait: bool = False) -> None:
    'Ingest local hook spans JSONL into raw_events with source/hash dedupe.'
    submit(db_path, '/api/maintenance/database/ingest', {'spans_file': str(spans_file.resolve())}, wait=wait)


@db.command("normalize")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--limit", type=int, default=None, help="Maximum pending raw_events to normalize.")
@click.option("--wait", is_flag=True)
def db_normalize(db_path: Path, limit: int | None, wait: bool = False) -> None:
    'Normalize pending raw_events into canonical SQLite tables.'
    submit(db_path, '/api/maintenance/database/normalize', {'limit': limit}, wait=wait)


@db.command("rebuild-graph")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--wait", is_flag=True)
def db_rebuild_graph(db_path: Path, wait: bool = False) -> None:
    'Rebuild graph_nodes and graph_edges from canonical SQLite tables.'
    submit(db_path, '/api/maintenance/database/graph', {}, wait=wait)


@db.command("rebuild-rollups")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--wait", is_flag=True)
def db_rebuild_rollups(db_path: Path, wait: bool = False) -> None:
    'Rebuild aggregate rollup tables from canonical SQLite tables.'
    submit(db_path, '/api/maintenance/database/rollups', {}, wait=wait)


@db.command("migrate")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
@click.option("--wait", is_flag=True)
def db_migrate(db_path: Path, wait: bool = False) -> None:
    'Apply pending SQLite migrations.'
    submit(db_path, '/api/maintenance/database/migrate', {}, wait=wait)


@db.command("doctor")
@click.option("--db-path", type=click.Path(path_type=Path), default=DEFAULT_DB_PATH)
def db_doctor(db_path: Path) -> None:
    'Inspect SQLite store migration, pragma, and foreign-key health.'
    payload = request(db_path, 'GET', '/api/database/health')
    echo_json(payload)
    if not payload.get('ok'):
        raise click.ClickException('SQLite store health checks failed')


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
@click.option("--wait", is_flag=True)
def db_prune_sessions(
    older_than_days: int,
    all_inactive_sessions: bool,
    apply_changes: bool,
    backup: bool,
    vacuum: bool,
    as_json: bool,
    db_path: Path,
    wait: bool = False) -> None:
    'Preview or prune inactive sessions selected by the retention policy.'
    submit(db_path, '/api/maintenance/database/prune', {'older_than_days': older_than_days, 'all_inactive_sessions': all_inactive_sessions, 'apply_changes': apply_changes, 'backup': backup, 'vacuum': vacuum}, wait=wait, as_json=as_json)
