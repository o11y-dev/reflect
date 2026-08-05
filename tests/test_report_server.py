from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reflect.core import _start_background_report_server, main
from reflect.report_server import ReportServerConfig, ReportServerDaemon, ReportServerStatus


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


def test_report_server_daemon_passes_refresh_to_child(tmp_path):
    config = ReportServerConfig(
        port=9876,
        db_path=tmp_path / "reflect.db",
        refresh=True,
    )
    daemon = ReportServerDaemon(config, state_dir=tmp_path / "state")
    with patch("reflect.report_server.subprocess.Popen") as popen, patch.object(
        daemon, "_port_in_use", return_value=False
    ):
        popen.return_value.pid = 4321

        daemon.start()

    command = popen.call_args.args[0]
    assert "--refresh" in command


def test_report_server_daemon_requests_refresh_from_running_server(tmp_path):
    daemon = _daemon(tmp_path)
    response = MagicMock()
    response.__enter__.return_value.read.return_value = (
        b'{"started":true,"preparation":{"state":"running"}}'
    )

    with patch(
        "reflect.report_server.urllib_request.urlopen",
        return_value=response,
    ) as urlopen:
        payload = daemon.request_refresh()

    request = urlopen.call_args.args[0]
    assert request.full_url == "http://127.0.0.1:9876/api/refresh"
    assert request.get_method() == "POST"
    assert payload["preparation"] == {"state": "running"}


def test_opening_reflect_upgrades_snapshot_only_server(tmp_path):
    db_path = tmp_path / "reflect.db"
    log_file = tmp_path / "report.log"
    snapshot_only = ReportServerStatus(
        running=True,
        pid=4321,
        port_in_use=False,
        url="http://127.0.0.1:8765/?report=api/data",
        log_file=log_file,
        db_path=db_path,
        refresh=False,
    )
    refreshed = ReportServerStatus(
        running=True,
        pid=4322,
        port_in_use=False,
        url=snapshot_only.url,
        log_file=log_file,
        db_path=db_path,
        refresh=True,
    )
    daemon = MagicMock()
    daemon.status.side_effect = [snapshot_only, refreshed]
    daemon.start.return_value = (4322, True)

    with patch("reflect.core._report_server_daemon", return_value=daemon):
        _start_background_report_server(db_path=db_path, refresh=True)

    daemon.stop.assert_called_once_with()
    daemon.start.assert_called_once_with()


def test_opening_reflect_refreshes_existing_refresh_server(tmp_path):
    db_path = tmp_path / "reflect.db"
    status = ReportServerStatus(
        running=True,
        pid=4321,
        port_in_use=False,
        url="http://127.0.0.1:8765/?report=api/data",
        log_file=tmp_path / "report.log",
        db_path=db_path,
        refresh=True,
    )
    daemon = MagicMock()
    daemon.status.return_value = status
    daemon.start.return_value = (4321, False)
    daemon.request_refresh.return_value = {
        "preparation": {"state": "running"},
    }

    with patch("reflect.core._report_server_daemon", return_value=daemon), patch(
        "webbrowser.open"
    ):
        _start_background_report_server(db_path=db_path, refresh=True)

    daemon.stop.assert_not_called()
    daemon.request_refresh.assert_called_once_with()


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


def test_direct_report_server_claims_state_without_opening_browser(tmp_path):
    from reflect.report_server import _run_daemon

    config = ReportServerConfig(
        port=9876,
        db_path=tmp_path / "reflect.db",
        refresh=True,
        open_browser=False,
    )
    daemon = ReportServerDaemon(config, state_dir=tmp_path / "state")
    pid_file = tmp_path / "state" / "report-server.pid"
    metadata_file = tmp_path / "state" / "report-server.json"

    def assert_state(**kwargs) -> None:
        assert int(pid_file.read_text()) == os.getpid()
        assert json.loads(metadata_file.read_text())["open_browser"] is False
        assert kwargs["open_browser"] is False

    with patch.object(daemon, "_port_in_use", return_value=False), patch(
        "reflect.core._run_browser_report",
        side_effect=assert_state,
    ):
        _run_daemon(config, daemon)

    assert not pid_file.exists()
    assert not metadata_file.exists()


def test_bare_reflect_detaches_report_server(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "reflect.db"
    with patch("reflect.core._start_background_report_server") as start, patch(
        "reflect.core._run_browser_report"
    ) as foreground:
        result = runner.invoke(main, ["--db-path", str(db_path)])

    assert result.exit_code == 0
    start.assert_called_once_with(db_path=db_path, otlp_traces=None, refresh=True)
    foreground.assert_not_called()


def test_server_start_defaults_to_snapshot_only(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "reflect.db"
    with patch("reflect.core._start_background_report_server") as start:
        result = runner.invoke(
            main,
            ["server", "--db-path", str(db_path), "start"],
        )

    assert result.exit_code == 0
    start.assert_called_once_with(
        db_path=db_path,
        otlp_traces=None,
        refresh=False,
    )


def test_server_start_can_opt_into_refresh(tmp_path):
    runner = CliRunner()
    db_path = tmp_path / "reflect.db"
    with patch("reflect.core._start_background_report_server") as start:
        result = runner.invoke(
            main,
            ["server", "--db-path", str(db_path), "--refresh", "start"],
        )

    assert result.exit_code == 0
    start.assert_called_once_with(
        db_path=db_path,
        otlp_traces=None,
        refresh=True,
    )


def test_server_otlp_source_requires_refresh(tmp_path):
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "server",
            "--db-path",
            str(tmp_path / "reflect.db"),
            "--otlp-traces",
            str(tmp_path / "traces.json"),
            "start",
        ],
    )

    assert result.exit_code == 2
    assert "--otlp-traces requires --refresh" in result.output


def test_server_commands_are_exposed():
    runner = CliRunner()
    for args in (["server", "--help"], ["server", "start", "--help"], ["server", "stop", "--help"], ["server", "status", "--help"]):
        result = runner.invoke(main, args)
        assert result.exit_code == 0
