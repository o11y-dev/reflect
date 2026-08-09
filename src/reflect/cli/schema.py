from __future__ import annotations

import json
from pathlib import Path

import click


@click.group()
def schema() -> None:
    """Schema and model tooling."""


@schema.command("export")
@click.option("--output", type=click.Path(path_type=Path), required=True)
def schema_export(output: Path) -> None:
    """Export Pydantic JSON Schema for core models."""
    from reflect.schema.events import RawEvent

    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "definitions": {"RawEvent": RawEvent.model_json_schema()},
    }
    output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    click.echo(f"Wrote schema to {output}")
