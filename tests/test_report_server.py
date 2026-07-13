from __future__ import annotations

import signal
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from reflect.core import main
from reflect.report_server import ReportServerConfig, ReportServerDaemon


def _daemon(tmp_path: Path) -> ReportServerDaemon:
    config = ReportServerConfig(port=9876, db_path=tmp_path / "reflect.db")
    return ReportServerDaemon(config, state_dir=tmp_path / "state")


def test_report_server_daemon_start_is_idempotent(tmp_path):
    daemon = _daemon(tmp_path)
    with patch("reflect.report_server.subprocess.Popen") as popen, patch(
        "reflect.report_server.os.kill"
    ) as kill, patch.object(daemon, "_port_in_use", return_value=False):
        popen.return_value.pid = 4321

        assert daemon.start() == (4321, True)
        assert daemon.start() == (4321, False)

    assert popen.call_count == 1
    kill.assert_called_once_with(4321, 0)
    assert daemon.status().url == "http://127.0.0.1:9876/?report=api/data"


def test_report_server_daemon_rejects_unmanaged_port_conflict(tmp_path):
    daemon = _daemon(tmp_path)
    with patch.object(daemon, "_port_in_use", return_value=True):
        try:
            daemon.start()
        except RuntimeError as exc:
            assert "Port 9876 is already in use" in str(exc)
        else:
            raise AssertionError("expected an unmanaged port conflict")


def test_report_server_status_reports_unmanaged_listener(tmp_path):
    daemon = _daemon(tmp_path)
    with patch.object(daemon, "_port_in_use", return_value=True):
        status = daemon.status()

    assert status.running is False
    assert status.pid is None
    assert status.port_in_use is True


def test_report_server_status_uses_persisted_runtime_config(tmp_path):
    started = _daemon(tmp_path)
    with patch("reflect.report_server.subprocess.Popen") as popen, patch(
        "reflect.report_server.os.kill"
    ), patch.object(started, "_port_in_use", return_value=False):
        popen.return_value.pid = 4321
        started.start()
        other_config = ReportServerConfig(port=8765, db_path=tmp_path / "other.db")
        status = ReportServerDaemon(other_config, state_dir=tmp_path / "state").status()

    assert status.url == "http://127.0.0.1:9876/?report=api/data"
    assert status.db_path == tmp_path / "reflect.db"


def test_report_server_daemon_status_cleans_stale_pid(tmp_path):
    daemon = _daemon(tmp_path)
    pid_file = tmp_path / "state" / "report-server.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("999999", encoding="utf-8")

    with patch("reflect.report_server.os.kill", side_effect=ProcessLookupError):
        status = daemon.status()

    assert status.running is False
    assert status.pid is None
    assert status.port_in_use is False
    assert not pid_file.exists()


def test_report_server_daemon_status_keeps_pid_when_probe_is_denied(tmp_path):
    daemon = _daemon(tmp_path)
    pid_file = tmp_path / "state" / "report-server.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("4321", encoding="utf-8")

    with patch("reflect.report_server.os.kill", side_effect=PermissionError):
        status = daemon.status()

    assert status.running is True
    assert status.pid == 4321
    assert status.port_in_use is False
    assert pid_file.exists()


def test_report_server_daemon_stop_terminates_process(tmp_path):
    daemon = _daemon(tmp_path)
    pid_file = tmp_path / "state" / "report-server.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("4321", encoding="utf-8")

    with patch(
        "reflect.report_server.os.kill",
        side_effect=[None, None, ProcessLookupError],
    ) as kill:
        assert daemon.stop() is True

    assert kill.call_args_list[1].args == (4321, signal.SIGTERM)
    assert not pid_file.exists()


def test_bare_reflect_detaches_report_server(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "reflect.db"
    with patch("reflect.core._start_background_report_server") as start, patch(
        "reflect.core._run_browser_report"
    ) as foreground:
        result = runner.invoke(main, ["--db-path", str(db_path)])

    assert result.exit_code == 0
    start.assert_called_once_with(db_path=db_path, otlp_traces=None)
    foreground.assert_not_called()


def test_server_commands_are_exposed():
    runner = CliRunner()
    for args in (["server", "--help"], ["server", "start", "--help"], ["server", "stop", "--help"], ["server", "status", "--help"]):
        result = runner.invoke(main, args)
        assert result.exit_code == 0
