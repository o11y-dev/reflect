"""Tests for Click CLI argument parsing and invocation."""

import json
import os
from datetime import datetime
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import make_span, wrap_otlp

import reflect.core as core
from reflect.core import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def otlp_file(tmp_path):
    spans = [make_span("UserPromptSubmit", input_tokens=100, output_tokens=50)]
    p = tmp_path / "traces.json"
    p.write_text(wrap_otlp(spans) + "\n")
    return p


class TestHelp:
    def test_help(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_setup_help(self, runner):
        result = runner.invoke(main, ["setup", "--help"])
        assert result.exit_code == 0
        assert "setup" in result.output.lower() or "Usage" in result.output

    def test_doctor_help(self, runner):
        result = runner.invoke(main, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output.lower() or "Usage" in result.output

    def test_update_help(self, runner):
        result = runner.invoke(main, ["update", "--help"])
        assert result.exit_code == 0
        assert "update" in result.output.lower() or "Usage" in result.output


class TestTerminalMode:
    def test_default_terminal_mode(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._render_terminal") as mock_render:
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
            assert result.exit_code == 0
            mock_render.assert_called_once()

    def test_no_terminal_saves_report(self, runner, otlp_file, tmp_path):
        output_path = tmp_path / "report.md"
        with patch("reflect.core.render_report") as mock_report:
            mock_report.return_value = "# report"
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--no-terminal",
                "--output", str(output_path),
            ])
            assert result.exit_code == 0
            mock_report.assert_called_once()


class TestPublishFlag:
    def test_publish_opens_browser(self, runner, otlp_file, tmp_path):
        # webbrowser.open is called only in --no-terminal --publish mode
        with patch("reflect.core.render_report") as mock_report, \
             patch("webbrowser.open") as mock_open:
            mock_report.return_value = "# report"
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--no-terminal",
                "--publish",
            ])
            assert result.exit_code == 0
            mock_open.assert_called_once()

    def test_dashboard_artifact_writes_json_and_uses_report_url(self, runner, otlp_file, tmp_path):
        artifact_path = tmp_path / "docs" / "reports" / "latest.json"
        with patch("reflect.core.render_report") as mock_report, \
             patch("webbrowser.open") as mock_open:
            mock_report.return_value = "# report"
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--no-terminal",
                "--dashboard-artifact", str(artifact_path),
                "--publish",
            ])
            assert result.exit_code == 0
            assert artifact_path.exists()
            payload = json.loads(artifact_path.read_text())
            assert "agents" in payload
            assert "?report=" in result.output
            mock_open.assert_called_once()


class TestNoDataNoCrash:
    def test_empty_dirs_no_crash(self, runner, tmp_path):
        with patch("reflect.core._render_terminal"):
            result = runner.invoke(main, [
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
            assert result.exit_code == 0


class TestUpdateAdvisor:
    def test_default_run_surfaces_startup_notice(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._render_terminal"), \
             patch("reflect.core._build_startup_update_notice", return_value="v9.9.9 is available. Run reflect doctor for details."):
            result = runner.invoke(main, [
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert "reflect notice:" in result.output
        assert "v9.9.9 is available" in result.output

    def test_doctor_renders_update_advisor(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (hook_home).mkdir(parents=True)
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "checked_at": "2025-01-01T00:00:00Z",
                "update_available": True,
                "source": "remote",
            },
            "local_issues": [
                {
                    "component": "Reflect skill copies",
                    "summary": "Global skill distribution is out of date for Claude Code.",
                    "remediation": "Run reflect setup from the workspace root to refresh installed skill copies.",
                }
            ],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core._collect_update_advisor", return_value=advisor):
            result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "Update advisor" in result.output
        assert "update available" in result.output
        assert "1.1.0" in result.output
        assert "workspace root" in result.output

    def test_update_apply_uses_pipx_upgrade(self, runner):
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": "1.1.0",
                "checked_at": "2025-01-01T00:00:00Z",
                "update_available": True,
                "source": "remote",
            },
            "local_issues": [],
        }
        with patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch("reflect.core.shutil.which", return_value="/usr/local/bin/pipx"), \
             patch("reflect.core.subprocess.check_call") as mock_check_call:
            result = runner.invoke(main, ["update", "--apply"])
        assert result.exit_code == 0
        mock_check_call.assert_called_once_with(["/usr/local/bin/pipx", "upgrade", "o11y-reflect"])
        assert "Package upgrade finished." in result.output

    def test_release_update_status_uses_cache_when_fresh(self, tmp_path):
        cache_path = tmp_path / "update-check.json"
        cache_path.write_text(json.dumps({
            "latest_version": "1.2.0",
            "checked_at": "2026-04-06T12:00:00Z",
        }))
        fake_now = datetime.fromisoformat("2026-04-06T13:00:00+00:00")

        with patch("reflect.core._UPDATE_CACHE_PATH", cache_path), \
             patch("reflect.core._current_reflect_version", return_value="1.0.0"), \
             patch("reflect.core._fetch_latest_reflect_version") as mock_fetch, \
             patch("reflect.core.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            status = core._release_update_status(allow_remote=True)

        assert status["latest_version"] == "1.2.0"
        assert status["update_available"] is True
        assert status["source"] == "cache"
        mock_fetch.assert_not_called()

    def test_startup_notice_ignores_hook_wiring_only(self):
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [
                {
                    "component": "Hook wiring",
                    "summary": "Claude Code hooks are incomplete.",
                    "remediation": "Run reflect setup.",
                }
            ],
        }

        assert core._build_startup_update_notice(advisor) is None

    def test_release_update_status_fetches_and_saves_remote_version(self, tmp_path):
        cache_path = tmp_path / "update-check.json"
        fake_now = datetime.fromisoformat("2026-04-06T13:00:00+00:00")

        with patch("reflect.core._UPDATE_CACHE_PATH", cache_path), \
             patch("reflect.core._current_reflect_version", return_value="1.0.0"), \
             patch("reflect.core._fetch_latest_reflect_version", return_value="1.3.0"), \
             patch("reflect.core.datetime") as mock_datetime:
            mock_datetime.now.return_value = fake_now
            mock_datetime.fromisoformat.side_effect = datetime.fromisoformat
            status = core._release_update_status(allow_remote=True)

        saved = json.loads(cache_path.read_text())
        assert status["latest_version"] == "1.3.0"
        assert status["source"] == "remote"
        assert saved["latest_version"] == "1.3.0"

    def test_detect_hook_drift_reports_missing_config(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core._claude_hooks_registered", return_value=False):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert drift["component"] == "Hook wiring"
        assert "missing" in drift["summary"]
        assert "reflect setup" in drift["remediation"]

    def test_publish_url_for_artifact_uses_docs_relative_ref(self, tmp_path):
        docs_dir = tmp_path / "docs"
        artifact_path = docs_dir / "reports" / "latest.json"
        artifact_path.parent.mkdir(parents=True)
        artifact_path.write_text("{}")

        publish_url = core._publish_url_for_artifact(artifact_path)

        assert publish_url == f"{(docs_dir / 'index.html').resolve().as_uri()}?report=reports/latest.json"


class TestDoctor:
    def test_doctor_reports_detected_agents_and_files(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        (reflect_home / "state" / "local_spans" / "s1.jsonl").write_text("{}\n")
        (reflect_home / "state" / "sessions" / "s1.json").write_text("{}\n")
        otlp_file = reflect_home / "state" / "otel-traces.json"
        otlp_file.write_text(wrap_otlp([make_span("UserPromptSubmit")]) + "\n")
        (reflect_home / "state" / "otel-logs.json").write_text('{"resourceLogs":[]}\n')
        (hook_home).mkdir(parents=True)
        (hook_home / "otel_config.json").write_text("{}\n")
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        advisor = {
            "release": {
                "current_version": "1.0.0",
                "latest_version": None,
                "checked_at": None,
                "update_available": False,
                "source": "unknown",
            },
            "local_issues": [],
        }
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"GEMINI_DIR": str(gemini_home)}, clear=False):
            result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "reflect doctor" in result.output
        assert "Telemetry files" in result.output
        assert "Detected agent homes" in result.output
        assert "Support matrix" in result.output
        assert "Gemini CLI" in result.output
        assert "Use native telemetry first" in result.output


class TestSetup:
    def test_setup_surfaces_detected_agent_guidance(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        gemini_home = tmp_path / ".gemini"
        gemini_home.mkdir()
        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch.dict(os.environ, {"HOME": str(home_dir), "GEMINI_DIR": str(gemini_home)}, clear=False):
            result = runner.invoke(main, ["setup"])
        assert result.exit_code == 0
        assert "native OTel" in result.output
        assert "Gemini" in result.output

    def test_setup_writes_agent_env_files_and_backups(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_home = home_dir / ".claude"
        copilot_home = home_dir / ".copilot"
        gemini_home = home_dir / ".gemini"
        vscode_settings = home_dir / "Library" / "Application Support" / "Code" / "User"

        claude_home.mkdir(parents=True)
        copilot_home.mkdir(parents=True)
        gemini_home.mkdir(parents=True)
        hook_home.mkdir(parents=True)
        vscode_settings.mkdir(parents=True)

        (claude_home / "settings.json").write_text('{"hooks":{}}\n')
        (gemini_home / "settings.json").write_text('{"telemetry":{"enabled":false,"outfile":".gemini/telemetry.log"}}\n')
        (hook_home / "otel_config.json").write_text('{"OTEL_EXPORTER_OTLP_ENDPOINT":"http://localhost:4317","OTEL_EXPORTER_OTLP_PROTOCOL":"grpc"}\n')
        (vscode_settings / "settings.json").write_text('{"github.copilot.chat.otel.enabled":false}\n')

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup"])

        assert result.exit_code == 0

        hook_backup_dir = reflect_home / "agents" / "opentelemetry-hooks" / "config-snapshots"
        claude_backup_dir = reflect_home / "agents" / "claude-code" / "config-snapshots"
        copilot_backup_dir = reflect_home / "agents" / "github-copilot" / "config-snapshots"
        gemini_backup_dir = reflect_home / "agents" / "gemini-cli" / "config-snapshots"

        # Claude Code: native OTel env block written to settings.json
        claude_settings = json.loads((claude_home / "settings.json").read_text())
        assert claude_settings["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert claude_settings["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
        assert claude_settings["env"]["OTEL_LOGS_EXPORTER"] == "otlp"

        # Gemini: native OTel settings written to settings.json
        gemini_settings = json.loads((gemini_home / "settings.json").read_text())
        assert gemini_settings["telemetry"]["enabled"] is True
        assert gemini_settings["telemetry"]["target"] == "local"
        assert gemini_settings["telemetry"]["useCollector"] is True
        assert gemini_settings["telemetry"]["otlpEndpoint"] == "http://localhost:4317"
        assert gemini_settings["telemetry"]["otlpProtocol"] == "grpc"
        assert gemini_settings["telemetry"]["logPrompts"] is False
        assert "outfile" not in gemini_settings["telemetry"]

        # Copilot VS Code: otel.* keys + CLI env vars written to settings.json
        copilot_settings = json.loads((vscode_settings / "settings.json").read_text())
        assert copilot_settings["github.copilot.chat.otel.enabled"] is True
        assert copilot_settings["github.copilot.chat.otel.otlpEndpoint"] == "http://localhost:4318"
        assert copilot_settings["github.copilot.chat.otel.exporterType"] == "otlp-http"
        assert copilot_settings["github.copilot.chat.otel.captureContent"] is False
        assert copilot_settings["env"]["COPILOT_OTEL_ENABLED"] == "true"
        assert copilot_settings["env"]["COPILOT_OTEL_OTLP_ENDPOINT"] == "http://localhost:4318"

        # Config snapshots created
        assert hook_backup_dir.exists()
        assert any(hook_backup_dir.iterdir())
        assert claude_backup_dir.exists()
        assert any(claude_backup_dir.iterdir())
        assert gemini_backup_dir.exists()
        assert any(gemini_backup_dir.iterdir())
        assert copilot_backup_dir.exists()
        assert any(copilot_backup_dir.iterdir())
