from __future__ import annotations

from contextlib import closing
from pathlib import Path

import click

from reflect.cli.common import REFLECT_HOME, echo_json, require_snapshot_schema


@click.command("impact")
@click.argument("measurement_id", required=False)
@click.option("--workflow-id", help="List checks for this workflow.")
@click.option("--limit", type=click.IntRange(1, 100), default=20, show_default=True)
@click.option("--json", "as_json", is_flag=True, help="Print the complete shared Impact payload.")
@click.option("--db-path", type=click.Path(path_type=Path), default=REFLECT_HOME / "state" / "reflect.db")
def impact(measurement_id: str | None, workflow_id: str | None, limit: int, as_json: bool, db_path: Path) -> None:
    """List persisted impact checks or inspect MEASUREMENT_ID without refreshing."""
    from reflect.context import ReflectContextService
    from reflect.store.sqlite import connect_sqlite_read_only

    require_snapshot_schema(db_path, refresh_hint=f"Run `reflect refresh --db-path {db_path}` first.")
    with closing(connect_sqlite_read_only(db_path, profile=None)) as conn:
        payload = ReflectContextService(conn, initialize_schema=False).impact(
            impact_id=measurement_id, workflow_id=workflow_id, limit=limit,
        )
    if as_json:
        echo_json(payload)
        return
    if payload.get("analysis_pending"):
        click.echo("Showing stored comparison evidence; current validation is pending.")
    if payload.get("found") is False:
        click.echo(f"Impact check not found: {measurement_id}")
        return
    checks = (
        [payload.get("impact_check") or payload["comparison"]]
        if measurement_id else payload["impact_checks"]
    )
    if not checks:
        click.echo("No persisted impact checks match this selection.")
        return
    for item in checks:
        cohort = item.get("cohort", item)
        state = cohort.get("measurement_state") or item.get("verdict") or "unknown"
        baseline = cohort.get("baseline_state") or "unknown"
        click.echo(f"{item['id']} · {state} · baseline: {baseline}")
        for reason in cohort.get("measurement_reasons") or []:
            click.echo(f"  {reason}")
    click.echo("Use --json for cohort counts, exposure, exclusion reasons, and the comparison ledger.")
