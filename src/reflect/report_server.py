from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ReportServerConfig:
    port: int
    db_path: Path
    otlp_traces: Path | None = None

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
        with self._log_file.open("a", encoding="utf-8") as log_fd:
            process = subprocess.Popen(
                command,
                stdout=log_fd,
                stderr=log_fd,
                start_new_session=True,
            )
        self._pid_file.write_text(str(process.pid), encoding="utf-8")
        self._metadata_file.write_text(
            json.dumps({
                "port": self.config.port,
                "db_path": str(self.config.db_path),
                "otlp_traces": str(self.config.otlp_traces) if self.config.otlp_traces else None,
            }),
            encoding="utf-8",
        )
        return process.pid, True

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
        )

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
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return self.config

    def _clear_state(self) -> None:
        self._pid_file.unlink(missing_ok=True)
        self._metadata_file.unlink(missing_ok=True)


def _run_daemon(config: ReportServerConfig, daemon: ReportServerDaemon) -> None:
    from reflect.core import _run_browser_report

    os.environ["REFLECT_PORT"] = str(config.port)
    try:
        _run_browser_report(
            otlp_traces=config.otlp_traces,
            sessions_dir=None,
            spans_dir=None,
            time_range="week",
            demo=False,
            dashboard_artifact=None,
            output=None,
            db_path=config.db_path,
        )
    finally:
        daemon.clear_pid(os.getpid())


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Reflect browser report server")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--otlp-traces", type=Path)
    args = parser.parse_args()
    config = ReportServerConfig(
        port=args.port,
        db_path=args.db_path,
        otlp_traces=args.otlp_traces,
    )
    state_dir = Path(os.environ.get("REFLECT_HOME", Path.home() / ".reflect")) / "state"
    _run_daemon(config, ReportServerDaemon(config, state_dir=state_dir))


if __name__ == "__main__":
    main()
