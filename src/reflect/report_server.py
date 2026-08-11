from __future__ import annotations

import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib import request as urllib_request

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from reflect.dashboard_server import start_publish_server
from reflect.parsing import _canonical_otlp_traces_path
from reflect.preparation import (
    PreparationCoordinator,
    PreparationRequest,
    PreparationScheduler,
    SQLiteSnapshotInspector,
)
from reflect.preparation_pipeline import prepare_sql_report_db
from reflect.raw_segments import readable_segment_paths
from reflect.store.sqlite import connect_sqlite_read_only

DEFAULT_REFRESH_INTERVAL_SECONDS = 5 * 60


def refresh_interval_from_env() -> int:
    raw = os.environ.get(
        "REFLECT_REFRESH_INTERVAL_SECONDS",
        str(DEFAULT_REFRESH_INTERVAL_SECONDS),
    )
    try:
        interval = int(raw)
    except ValueError as exc:
        raise RuntimeError(
            "REFLECT_REFRESH_INTERVAL_SECONDS must be a positive integer"
        ) from exc
    if interval <= 0:
        raise RuntimeError(
            "REFLECT_REFRESH_INTERVAL_SECONDS must be a positive integer"
        )
    return interval


@dataclass(frozen=True)
class ReportServerConfig:
    port: int
    db_path: Path
    otlp_traces: Path | None = None
    refresh: bool = False
    open_browser: bool = True
    refresh_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?report=api/data"


@dataclass(frozen=True)
class ReportServerStatus:
    running: bool
    pid: int | None
    port_in_use: bool
    url: str
    log_file: Path
    db_path: Path
    refresh: bool
    refresh_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS


class ReportServerDaemon:
    """Own the detached browser-report server lifecycle."""

    def __init__(self, config: ReportServerConfig, *, state_dir: Path) -> None:
        self.config = config
        self._pid_file = state_dir / "report-server.pid"
        self._metadata_file = state_dir / "report-server.json"
        self._log_file = state_dir / "report-server.log"

    def start(self) -> tuple[int, bool]:
        existing = self._running_pid()
        if existing is not None:
            return existing, False
        if self._port_in_use():
            raise RuntimeError(
                f"Port {self.config.port} is already in use by an unmanaged process"
            )

        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "reflect.report_server",
            "--port",
            str(self.config.port),
            "--db-path",
            str(self.config.db_path),
        ]
        if self.config.otlp_traces is not None:
            command.extend(["--otlp-traces", str(self.config.otlp_traces)])
        if self.config.refresh:
            command.extend(
                [
                    "--refresh",
                    "--refresh-interval-seconds",
                    str(self.config.refresh_interval_seconds),
                ]
            )
        with self._log_file.open("a", encoding="utf-8") as log_fd:
            process = subprocess.Popen(
                command,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
            )
        self._record_state(process.pid)
        return process.pid, True

    def claim_current_process(self) -> None:
        """Claim lifecycle state when an OS user service launches us directly."""
        current_pid = os.getpid()
        existing = self._running_pid()
        if existing is not None and existing != current_pid:
            raise RuntimeError(f"Report server already running (PID {existing})")
        if existing is None and self._port_in_use():
            raise RuntimeError(
                f"Port {self.config.port} is already in use by an unmanaged process"
            )
        self._pid_file.parent.mkdir(parents=True, exist_ok=True)
        self._record_state(current_pid)

    def _record_state(self, pid: int) -> None:
        self._pid_file.write_text(str(pid), encoding="utf-8")
        self._metadata_file.write_text(
            json.dumps({
                "port": self.config.port,
                "db_path": str(self.config.db_path),
                "otlp_traces": str(self.config.otlp_traces) if self.config.otlp_traces else None,
                "refresh": self.config.refresh,
                "refresh_interval_seconds": self.config.refresh_interval_seconds,
                "open_browser": self.config.open_browser,
            }),
            encoding="utf-8",
        )

    def stop(self, *, timeout: float = 3.0) -> bool:
        pid = self._running_pid()
        if pid is None:
            return False
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
        self._clear_state()
        return True

    def status(self) -> ReportServerStatus:
        pid = self._running_pid()
        config = self._stored_config() if pid is not None else self.config
        return ReportServerStatus(
            running=pid is not None,
            pid=pid,
            port_in_use=pid is None and self._port_in_use(),
            url=config.url,
            log_file=self._log_file,
            db_path=config.db_path,
            refresh=config.refresh,
            refresh_interval_seconds=config.refresh_interval_seconds,
        )

    def request_refresh(self, *, timeout: float = 2.0) -> dict[str, object]:
        """Ask an existing refresh-capable server to prepare a new snapshot."""
        request = urllib_request.Request(
            f"http://127.0.0.1:{self.config.port}/api/refresh",
            data=b"",
            headers={"Accept": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
        if not isinstance(payload, dict):
            raise RuntimeError("Report server returned an invalid refresh response")
        return payload

    def clear_pid(self, pid: int) -> None:
        if self._read_pid() == pid:
            self._clear_state()

    def _running_pid(self) -> int | None:
        pid = self._read_pid()
        if pid is None:
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            self._clear_state()
            return None
        except PermissionError:
            return pid
        return pid

    def _read_pid(self) -> int | None:
        if not self._pid_file.exists():
            return None
        try:
            return int(self._pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            self._clear_state()
            return None

    def _port_in_use(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            return probe.connect_ex(("127.0.0.1", self.config.port)) == 0

    def _stored_config(self) -> ReportServerConfig:
        try:
            payload = json.loads(self._metadata_file.read_text(encoding="utf-8"))
            otlp_traces = payload.get("otlp_traces")
            return ReportServerConfig(
                port=int(payload["port"]),
                db_path=Path(payload["db_path"]),
                otlp_traces=Path(otlp_traces) if otlp_traces else None,
                refresh=bool(payload.get("refresh", False)),
                refresh_interval_seconds=int(
                    payload.get(
                        "refresh_interval_seconds",
                        DEFAULT_REFRESH_INTERVAL_SECONDS,
                    )
                ),
                open_browser=bool(payload.get("open_browser", True)),
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self.config

    def _clear_state(self) -> None:
        self._pid_file.unlink(missing_ok=True)
        self._metadata_file.unlink(missing_ok=True)


def default_otlp_traces() -> Path | None:
    """Return the canonical raw trace path when any readable segment exists."""
    path = _canonical_otlp_traces_path()
    return path if readable_segment_paths(path) else None


def has_sql_report_snapshot(db_path: Path) -> bool:
    if not SQLiteSnapshotInspector(db_path).inspect().ready:
        return False
    try:
        conn = connect_sqlite_read_only(db_path)
        try:
            return bool(conn.execute("SELECT 1 FROM session_rollups LIMIT 1").fetchone())
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def render_preparation_summary(
    console: Console,
    preparation: dict[str, object],
) -> None:
    ingest = preparation["ingest"]
    normalize = preparation["normalize"]
    rollups = preparation["rollups"]
    assert isinstance(ingest, dict)
    assert isinstance(normalize, dict)
    assert isinstance(rollups, dict)
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="bold")
    summary.add_column(justify="right")
    for label, value in (
        ("Inserted", ingest["inserted"]),
        ("Skipped", ingest["skipped"]),
        ("Normalized", normalize["processed"]),
        ("Sessions", rollups["session_rollups"]),
    ):
        summary.add_row(label, f"{int(value):,}")
    console.print(
        Panel(
            summary,
            title="[bold orange3]REFLECT[/bold orange3]",
            border_style="orange3",
        )
    )


def run_browser_report(
    *,
    otlp_traces: Path | None,
    demo: bool,
    db_path: Path,
    refresh: bool,
    open_browser: bool = True,
    refresh_interval_seconds: int = DEFAULT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Prepare and serve the browser report without depending on the CLI root."""
    if refresh_interval_seconds <= 0:
        raise ValueError("refresh_interval_seconds must be positive")

    console = Console()
    include_native_sessions = False
    if refresh:
        if demo:
            demo_traces = Path(__file__).parent / "data" / "demo-traces.json"
            if not demo_traces.exists():
                demo_traces = Path(__file__).resolve().parents[2] / "state" / "demo-traces.json"
            otlp_traces = demo_traces if demo_traces.exists() else otlp_traces
        elif otlp_traces is None:
            otlp_traces = default_otlp_traces()
            include_native_sessions = True
        else:
            include_native_sessions = (
                otlp_traces.expanduser().resolve()
                == _canonical_otlp_traces_path().expanduser().resolve()
            )

    requires_fresh_snapshot = not has_sql_report_snapshot(db_path)
    prepared_synchronously = False
    if not refresh:
        if requires_fresh_snapshot:
            raise click.ClickException(
                f"No report snapshot found at {db_path}. Enable refresh to create one."
            )
        click.echo("Serving the current snapshot without refreshing telemetry.")
    elif requires_fresh_snapshot:
        with console.status("[bold orange3]reflecting...[/bold orange3]", spinner="dots"):
            preparation = prepare_sql_report_db(
                db_path,
                otlp_traces=otlp_traces,
                include_native_sessions=include_native_sessions,
            )
        render_preparation_summary(console, preparation)
        prepared_synchronously = True

    scheduler = None
    if refresh:
        def prepare_in_background(progress):
            return prepare_sql_report_db(
                db_path,
                otlp_traces=otlp_traces,
                include_native_sessions=include_native_sessions,
                progress=progress,
            )

        coordinator = PreparationCoordinator(
            prepare_in_background,
            request=PreparationRequest(
                sources=tuple(
                    source
                    for source, enabled in (
                        ("otlp", otlp_traces is not None),
                        ("native_sessions", include_native_sessions),
                    )
                    if enabled
                ),
            ),
        )
        scheduler = PreparationScheduler(
            coordinator,
            interval_seconds=refresh_interval_seconds,
            run_immediately=not prepared_synchronously,
        )
        click.echo(
            "Serving the current snapshot; refreshing telemetry automatically "
            f"every {refresh_interval_seconds} seconds."
        )

    from reflect.gateway import raw_storage_status

    start_publish_server(
        db_path=db_path,
        preparation_scheduler=scheduler,
        raw_storage_status_loader=raw_storage_status,
        open_browser=open_browser,
    )


def _run_daemon(config: ReportServerConfig, daemon: ReportServerDaemon) -> None:
    os.environ["REFLECT_PORT"] = str(config.port)
    daemon.claim_current_process()
    try:
        run_browser_report(
            otlp_traces=config.otlp_traces,
            demo=False,
            db_path=config.db_path,
            refresh=config.refresh,
            open_browser=config.open_browser,
            refresh_interval_seconds=config.refresh_interval_seconds,
        )
    finally:
        daemon.clear_pid(os.getpid())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Reflect browser report server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--otlp-traces", type=Path)
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--refresh-interval-seconds",
        type=int,
        default=refresh_interval_from_env(),
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_false",
        dest="open_browser",
        help="Serve without opening a browser window.",
    )
    args = parser.parse_args()
    config = ReportServerConfig(
        port=args.port,
        db_path=args.db_path,
        otlp_traces=args.otlp_traces,
        refresh=args.refresh,
        refresh_interval_seconds=args.refresh_interval_seconds,
        open_browser=args.open_browser,
    )
    state_dir = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")) / "state"
    _run_daemon(config, ReportServerDaemon(config, state_dir=state_dir))


if __name__ == "__main__":
    main()
