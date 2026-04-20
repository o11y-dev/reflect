"""Tests for Click CLI argument parsing and invocation."""

import io
import json
import os
import tomllib
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

    def test_report_help(self, runner):
        result = runner.invoke(main, ["report", "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_skills_help(self, runner):
        result = runner.invoke(main, ["skills", "--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output


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


class TestReportSubcommand:
    def test_report_starts_server(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server") as mock_server:
            result = runner.invoke(main, [
                "report",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        mock_server.assert_called_once()

    def test_report_with_output_saves_markdown(self, runner, otlp_file, tmp_path):
        with patch("reflect.core._start_publish_server"), \
             patch("reflect.core.render_report") as mock_report:
            mock_report.return_value = "# report"
            result = runner.invoke(main, [
                "report",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--output", str(tmp_path / "report.md"),
            ])
        assert result.exit_code == 0
        mock_report.assert_called_once()

    def test_report_with_dashboard_artifact_writes_json(self, runner, otlp_file, tmp_path):
        artifact_path = tmp_path / "docs" / "reports" / "latest.json"
        with patch("reflect.core._start_publish_server"):
            result = runner.invoke(main, [
                "report",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
                "--dashboard-artifact", str(artifact_path),
            ])
        assert result.exit_code == 0
        assert artifact_path.exists()
        assert "agents" in json.loads(artifact_path.read_text())


_FAKE_SKILLS = [
    {"name": "debug-loop", "description": "Iterative debug workflow", "content": "## Steps\n1. Do the thing"},
    {"name": "context-reset", "description": "Clear and re-establish scope", "content": "## Steps\n1. Reset"},
    {"name": "test-first", "description": "Write tests before fixing", "content": "## Steps\n1. Test"},
]
_R = lambda code, out, err="": type("R", (), {"returncode": code, "stdout": out, "stderr": err})()  # noqa: E731


class TestSkillsSubcommand:
    def _agent_fixture(self, skill_dest, fake_skills=None):
        return [{
            "name": "Claude Code",
            "detected": True,
            "global_path": str(skill_dest),
            "skill_path": ".claude/skills/",
        }]

    def test_skills_writes_all_with_yes(self, runner, otlp_file, tmp_path):
        skill_dest = tmp_path / "skills"
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert (skill_dest / "debug-loop" / "SKILL.md").exists()
        assert "name: debug-loop" in (skill_dest / "debug-loop" / "SKILL.md").read_text()

    def test_skills_partial_selection(self, runner, otlp_file, tmp_path):
        """User selects only skill #1 from a list of 3."""
        skill_dest = tmp_path / "skills"
        fake_output = json.dumps(_FAKE_SKILLS)
        with patch("subprocess.run", return_value=_R(0, fake_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            # --yes is NOT passed; input "1" selects only the first skill, then "y" confirms
            result = runner.invoke(main, [
                "skills", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ], input="1\ny\n")
        assert result.exit_code == 0
        assert (skill_dest / "debug-loop" / "SKILL.md").exists()
        assert not (skill_dest / "context-reset").exists()
        assert not (skill_dest / "test-first").exists()

    def test_skills_gemini_uses_p_flag(self, runner, otlp_file, tmp_path):
        """gemini agent uses -p flag, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "gemini",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gemini"
        assert "-p" in cmd
        assert "--print" not in cmd

    def test_skills_cursor_agent_uses_print_flag(self, runner, otlp_file, tmp_path):
        """cursor-agent uses --print flag."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "cursor-agent",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "cursor-agent"
        assert "--print" in cmd

    def test_skills_copilot_uses_prompt_flag(self, runner, otlp_file, tmp_path):
        """copilot uses --prompt flag, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "copilot",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "copilot"
        assert "--prompt" in cmd
        assert "--print" not in cmd

    def test_skills_opencode_uses_run_subcommand(self, runner, otlp_file, tmp_path):
        """opencode uses 'run' subcommand, not --print."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]):
            runner.invoke(main, [
                "skills", "--yes", "--agent", "opencode",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "opencode"
        assert "run" in cmd
        assert "--print" not in cmd

    def test_skills_autodetect_picks_first_available(self, runner, otlp_file, tmp_path):
        """With no --agent, auto-detection uses the first CLI found by shutil.which."""
        fake_output = json.dumps([_FAKE_SKILLS[0]])
        with patch("subprocess.run", return_value=_R(0, fake_output)) as mock_run, \
             patch("reflect.core._detect_agents", return_value=[]), \
             patch("reflect.core.shutil.which", side_effect=lambda b: "/usr/bin/gemini" if b == "gemini" else None):
            runner.invoke(main, [
                "skills", "--yes",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gemini"
        assert "-p" in cmd

    def test_skills_no_agent_available_exits(self, runner, otlp_file, tmp_path):
        with patch("reflect.core.shutil.which", return_value=None):
            result = runner.invoke(main, [
                "skills",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_agent_failure_exits_nonzero(self, runner, otlp_file, tmp_path):
        with patch("subprocess.run", return_value=_R(1, "", "error")), \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_bad_json_exits_nonzero(self, runner, otlp_file, tmp_path):
        with patch("subprocess.run", return_value=_R(0, "not json")), \
             patch("reflect.core._detect_agents", return_value=[]):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code != 0

    def test_skills_strips_json_fences(self, runner, otlp_file, tmp_path):
        """Agent output wrapped in ```json fences is parsed correctly."""
        skill_dest = tmp_path / "skills"
        fenced_output = '```json\n' + json.dumps([_FAKE_SKILLS[0]]) + '\n```'
        with patch("subprocess.run", return_value=_R(0, fenced_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert (skill_dest / "debug-loop" / "SKILL.md").exists()

    def test_skills_strips_plain_fences(self, runner, otlp_file, tmp_path):
        """Agent output wrapped in ``` fences (no language tag) is parsed correctly."""
        skill_dest = tmp_path / "skills"
        fenced_output = '```\n' + json.dumps([_FAKE_SKILLS[0]]) + '\n```'
        with patch("subprocess.run", return_value=_R(0, fenced_output)), \
             patch("reflect.core._detect_agents", return_value=self._agent_fixture(skill_dest)):
            result = runner.invoke(main, [
                "skills", "--yes", "--agent", "claude",
                "--otlp-traces", str(otlp_file),
                "--sessions-dir", str(tmp_path / "s"),
                "--spans-dir", str(tmp_path / "sp"),
            ])
        assert result.exit_code == 0
        assert (skill_dest / "debug-loop" / "SKILL.md").exists()


def test_strip_json_fences_variants():
    from reflect.core import _strip_json_fences

    raw = '[{"name": "x"}]'

    # No fences -- returns as-is
    assert _strip_json_fences(raw) == raw

    # ```json fences
    assert _strip_json_fences(f'```json\n{raw}\n```') == raw

    # ``` fences (no language tag)
    assert _strip_json_fences(f'```\n{raw}\n```') == raw

    # Leading/trailing whitespace around fences
    assert _strip_json_fences(f'  \n```json\n{raw}\n```\n  ') == raw

    # Prose before/after fences (only content between fences extracted)
    assert _strip_json_fences(f'Here is the JSON:\n```json\n{raw}\n```\nDone.') == raw

    # Backticks inside JSON strings are not treated as closing fence
    with_backticks = '[{"name": "x", "content": "```python\\nprint()\\n```"}]'
    fenced = f'```json\n{with_backticks}\n```'
    assert _strip_json_fences(fenced) == with_backticks


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
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=False):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert drift["component"] == "Hook wiring"
        assert "missing" in drift["summary"]
        assert "reflect setup" in drift["remediation"]

    def test_detect_hook_drift_returns_none_when_otel_hook_not_installed(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value=None):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_skips_when_otel_hook_not_installed_despite_config(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value=None):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_returns_none_when_fully_configured(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is None

    def test_detect_hook_drift_reports_missing_endpoint(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text('{"IDE_OTEL_LOCAL_SPANS": "true"}')

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "OTEL_EXPORTER_OTLP_ENDPOINT" in drift["summary"]

    def test_detect_hook_drift_reports_missing_protocol(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text(
            '{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317"}'
        )

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in drift["summary"]

    def test_detect_hook_drift_reports_unsupported_protocol(self, tmp_path):
        hook_home = tmp_path / ".otel-hook-home"
        hook_home.mkdir()
        (hook_home / "otel_config.json").write_text(
            '{"IDE_OTEL_LOCAL_SPANS": "true", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317", "OTEL_EXPORTER_OTLP_PROTOCOL": "json"}'
        )

        with patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core._claude_hooks_registered", return_value=True):
            drift = core._detect_hook_drift()

        assert drift is not None
        assert "unsupported value" in drift["summary"]

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

    def test_doctor_support_matrix_marks_planned_agents(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        windsurf_home = tmp_path / ".codeium" / "windsurf"
        windsurf_home.mkdir(parents=True)
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
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
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Windsurf" in result.output
        assert "Planned" in result.output

    def test_doctor_otlp_logs_waiting_when_otel_hook_installed(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        hook_home.mkdir(parents=True)
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
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "waiting" in result.output

    def test_doctor_otlp_logs_missing_when_otel_hook_not_installed(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        (reflect_home / "state").mkdir(parents=True)
        (reflect_home / "state" / "local_spans").mkdir(parents=True)
        (reflect_home / "state" / "sessions").mkdir(parents=True)
        hook_home.mkdir(parents=True)
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
             patch("reflect.core.shutil.which", return_value=None), \
             patch("reflect.core._collect_update_advisor", return_value=advisor), \
             patch.dict(os.environ, {"HOME": str(tmp_path)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        # Without otel-hook, OTLP logs should show "missing" not "waiting"
        assert "waiting" not in result.output

    def test_doctor_shows_native_agent_telemetry_status(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        claude_settings = home_dir / ".claude" / "settings.json"
        claude_settings.parent.mkdir(parents=True)
        claude_settings.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            }
        }))
        (reflect_home / "state").mkdir(parents=True)
        hook_home.mkdir(parents=True)
        (hook_home / "otel_config.json").write_text(json.dumps({
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }))
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
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0
        assert "Native agent telemetry" in result.output
        assert "Claude Code" in result.output
        assert "incomplete" in result.output


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
             patch("reflect.core._distribute_skills"), \
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


    def test_setup_seeds_config_from_example_on_fresh_install(self, runner, tmp_path):
        reflect_home = tmp_path / ".reflect"
        hook_home = tmp_path / ".otel-hook-home"
        home_dir = tmp_path / "home"
        hook_home.mkdir(parents=True)

        example_config = {
            "_comment_endpoint": "Set your OTLP endpoint here",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        }
        (hook_home / "otel_config.example.json").write_text(
            json.dumps(example_config, indent=2) + "\n"
        )

        with patch("reflect.core.REFLECT_HOME", reflect_home), \
             patch("reflect.core.HOOK_HOME", hook_home), \
             patch("reflect.core.shutil.which", return_value="/usr/bin/otel-hook"), \
             patch("reflect.core.subprocess.check_call"), \
             patch("reflect.core._distribute_skills"), \
             patch.dict(os.environ, {"HOME": str(home_dir)}, clear=False):
            result = runner.invoke(main, ["setup"])

        assert result.exit_code == 0

        config_path = hook_home / "otel_config.json"
        assert config_path.exists(), "otel_config.json should be written on fresh install"

        written = json.loads(config_path.read_text())
        # Sentinel key from example must be preserved
        assert "_comment_endpoint" in written, "Example sentinel key must be seeded into config"
        assert written["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
        # IDE_OTEL_LOCAL_SPANS must be forced to "true"
        assert written["IDE_OTEL_LOCAL_SPANS"] == "true"


class TestNativeOtelConfig:
    """Unit tests for per-agent native OTel configuration functions."""

    HOOK_CFG = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    }

    def _console(self):
        from rich.console import Console
        return Console(file=io.StringIO())

    # ------------------------------------------------------------------
    # Claude Code
    # ------------------------------------------------------------------

    def test_claude_native_otel_creates_env_block(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text('{"hooks":{}}\n')

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        result = json.loads(settings_file.read_text())
        assert result["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert result["env"]["OTEL_METRICS_EXPORTER"] == "otlp"
        assert result["env"]["OTEL_LOGS_EXPORTER"] == "otlp"
        assert result["env"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"
        assert result["env"]["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "cumulative"

    def test_claude_native_otel_idempotent(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
                "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "cumulative",
            }
        }))
        content_before = settings_file.read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        assert settings_file.read_text() == content_before

    def test_claude_native_otel_creates_file_if_missing(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

        settings_file = tmp_path / ".claude" / "settings.json"
        assert settings_file.exists()
        result = json.loads(settings_file.read_text())
        assert result["env"]["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"

    def test_claude_native_otel_read_error(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text("not valid json {{{{")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            # Should not raise
            core._configure_claude_native_otel(self._console(), self.HOOK_CFG)

    # ------------------------------------------------------------------
    # Copilot CLI
    # ------------------------------------------------------------------

    def test_copilot_cli_native_otel_writes_env_block(self, tmp_path):
        vscode = tmp_path / ".config" / "Code" / "User"
        vscode.mkdir(parents=True)
        (vscode / "settings.json").write_text('{"github.copilot.chat.otel.enabled": true}\n')

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(self._console(), self.HOOK_CFG)

        result = json.loads((vscode / "settings.json").read_text())
        assert result["env"]["COPILOT_OTEL_ENABLED"] == "true"
        assert result["env"]["COPILOT_OTEL_OTLP_ENDPOINT"] == "http://localhost:4318"

    def test_copilot_cli_native_otel_no_settings_file(self, tmp_path):
        from rich.console import Console

        stream = io.StringIO()
        console = Console(file=stream)
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(console, self.HOOK_CFG)

        output = stream.getvalue()
        assert "Skipped Copilot CLI OTel env vars" in output
        assert "settings.json" in output

    def test_copilot_native_otel_no_settings_file_surfaces_skip(self, tmp_path):
        from rich.console import Console

        stream = io.StringIO()
        console = Console(file=stream)
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_native_otel(console, self.HOOK_CFG)

        output = stream.getvalue()
        assert "Skipped native Copilot OTel" in output
        assert "settings.json" in output

    def test_copilot_cli_native_otel_idempotent(self, tmp_path):
        vscode = tmp_path / ".config" / "Code" / "User"
        vscode.mkdir(parents=True)
        settings = {
            "env": {
                "COPILOT_OTEL_ENABLED": "true",
                "COPILOT_OTEL_OTLP_ENDPOINT": "http://localhost:4318",
            }
        }
        (vscode / "settings.json").write_text(json.dumps(settings))
        content_before = (vscode / "settings.json").read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_copilot_cli_native_otel(self._console(), self.HOOK_CFG)

        assert (vscode / "settings.json").read_text() == content_before

    # ------------------------------------------------------------------
    # Codex CLI
    # ------------------------------------------------------------------

    def test_codex_native_otel_creates_config(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        config_path = tmp_path / ".codex" / "config.toml"
        config = config_path.read_text()
        parsed = tomllib.loads(config)
        assert "[otel]" in config
        assert parsed["otel"]["exporter"]["otlp-grpc"]["endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["traces_exporter"] == "otlp"
        assert parsed["otel"]["traces_endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["logs_exporter"] == "otlp"
        assert parsed["otel"]["logs_endpoint"] == "http://localhost:4317"
        assert parsed["otel"]["log_user_prompt"] is False

    def test_codex_native_otel_appends_to_existing_config(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[model]\nname = \"o3\"\n")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        config = (codex_dir / "config.toml").read_text()
        assert "[model]" in config  # existing section preserved
        assert "[otel]" in config
        assert 'traces_exporter = "otlp"' in config
        assert 'logs_exporter = "otlp"' in config
        assert "log_user_prompt = false" in config

    def test_codex_native_otel_already_configured(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        config_text = (
            '[otel]\nexporter = {otlp-grpc = {endpoint = "http://localhost:4317"}}\n'
            'traces_exporter = "otlp"\n'
            'traces_endpoint = "http://localhost:4317"\n'
            'logs_exporter = "otlp"\n'
            'logs_endpoint = "http://localhost:4317"\n'
            "log_user_prompt = false\n"
        )
        (codex_dir / "config.toml").write_text(config_text)
        content_before = (codex_dir / "config.toml").read_text()

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        assert (codex_dir / "config.toml").read_text() == content_before

    def test_codex_native_otel_replaces_incomplete_section(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[model]\nname = \"o3\"\n\n[otel]\ntraces_exporter = \"otlp\"\n"
        )

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        parsed = tomllib.loads((codex_dir / "config.toml").read_text())
        assert parsed["model"]["name"] == "o3"
        assert parsed["otel"]["logs_exporter"] == "otlp"
        assert parsed["otel"]["logs_endpoint"] == "http://localhost:4317"

    def test_codex_native_otel_replaces_mid_file_section_without_clobbering_following_sections(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(
            "[model]\nname = \"o3\"\n\n"
            "[otel]\ntraces_exporter = \"console\"\n\n"
            "[projects]\n\"/tmp/demo\" = {trust_level = \"trusted\"}\n"
        )

        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

        updated = (codex_dir / "config.toml").read_text()
        parsed = tomllib.loads(updated)
        assert parsed["otel"]["traces_exporter"] == "otlp"
        assert parsed["otel"]["logs_exporter"] == "otlp"
        assert parsed["projects"]["/tmp/demo"]["trust_level"] == "trusted"
        assert "\n\n[projects]\n" in updated

    def test_codex_native_otel_read_error(self, tmp_path):
        codex_dir = tmp_path / ".codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text("[[[[invalid toml")

        with patch("reflect.core.Path.home", return_value=tmp_path):
            # Should not raise
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)

    def test_native_otel_status_reports_incomplete_claude_config(self, tmp_path):
        settings_file = tmp_path / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True)
        settings_file.write_text(json.dumps({
            "env": {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOGS_EXPORTER": "otlp",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            }
        }))

        with patch("reflect.core.Path.home", return_value=tmp_path):
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG)

        claude = next(status for status in statuses if status["agent"] == "Claude Code")
        assert claude["status"] == "incomplete"
        assert "OTEL_EXPORTER_OTLP_PROTOCOL" in claude["details"]

    def test_native_otel_status_reports_ready_codex_config(self, tmp_path):
        with patch("reflect.core.Path.home", return_value=tmp_path):
            core._configure_codex_native_otel(self._console(), self.HOOK_CFG)
            statuses = core._collect_native_otel_statuses(self.HOOK_CFG)

        codex = next(status for status in statuses if status["agent"] == "OpenAI Codex CLI")
        assert codex["status"] == "ready"
        assert "trace/log OTLP exporters" in codex["details"]
