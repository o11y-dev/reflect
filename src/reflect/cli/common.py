from __future__ import annotations

import json
import os
from pathlib import Path

import click

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))


def echo_json(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def require_snapshot_schema(db_path: Path, *, refresh_hint: str) -> None:
    from reflect.preparation import (
        CommandPreparationPolicy,
        SnapshotLifecycleService,
        SnapshotUnavailableError,
        SQLiteSnapshotInspector,
    )

    lifecycle = SnapshotLifecycleService(
        SQLiteSnapshotInspector(db_path, profile=None),
        policy=CommandPreparationPolicy(require_sessions=False),
        refresh_hint=refresh_hint,
    )
    try:
        lifecycle.prepare(requested_refresh=None)
    except SnapshotUnavailableError as exc:
        raise click.ClickException(str(exc)) from exc
