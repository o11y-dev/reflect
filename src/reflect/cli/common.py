from __future__ import annotations

import json
import os
from pathlib import Path

import click

REFLECT_HOME = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect"))


def echo_json(payload: object) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
